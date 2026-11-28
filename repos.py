"""GitHub repository registration, Hermes Project anchoring, and PR gate operations."""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable, Optional

from .config import (
    DESIGN_APPROVAL_POLICIES,
    DESIGN_PROVIDERS,
    MERGE_POLICIES,
    PUBLISH_POLICIES,
    RIGOR_LEVELS,
    get_repo,
    list_repos,
    load_settings,
    repos_store_lock,
    resolve_publish_policy,
    save_settings,
    single_line,
)
from .git_cli import git_err, run_gh, run_git
from .git_cli import parse_status_z as parse_status_z  # public re-export
from .metrics import capture_metrics_snapshot
from .store_kanban import open_kanban_ro, resolve_kanban_db_path
from .validators import (
    OWNER_REPO_RE,
    require_clean_str,
    validate_branch_field,
    validate_default_branch,
    validate_full_sha,
    validate_git_ref_name,
    validate_pr_number,
)
from .validators import normalize_repo as normalize_repo  # public re-export
from .vocabulary import INACTIVE_CARD_STATUSES
from .vocabulary import WORKTREE_BUCKETS as WORKTREE_BUCKETS  # public re-export


class RepoNotFoundError(LookupError):
    """Raised when a repository slug or `owner/repo` is not registered with Factonio."""


_UNSET: Any = object()

EDITABLE_REPO_FIELDS: tuple[str, ...] = (
    "merge_policy",
    "publish_policy",
    "max_fix_rounds",
    "default_branch",
    "design_provider",
    "design_approval",
    "default_rigor",
)

# Fields whose `None` (inherit) value removes the per-repo override instead of storing null.
_CLEARABLE_REPO_FIELDS = frozenset({"design_provider", "design_approval", "default_rigor"})

_INHERIT_TOKENS = frozenset({"", "inherit", "default"})


def _validate_merge_policy(value: Any) -> str:
    if isinstance(value, str):
        norm = value.strip().lower()
        if norm in MERGE_POLICIES:
            return norm
    raise ValueError(f"merge_policy must be one of {', '.join(MERGE_POLICIES)}; got {value!r}")


def _validate_publish_policy(value: Any) -> str:
    if isinstance(value, str):
        norm = value.strip().lower()
        if norm in PUBLISH_POLICIES:
            return norm
    raise ValueError(f"publish_policy must be one of {', '.join(PUBLISH_POLICIES)}; got {value!r}")


def _validate_max_fix_rounds(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"max_fix_rounds must be an integer >= 1; got {value!r}")
    if value < 1:
        raise ValueError(f"max_fix_rounds must be an integer >= 1; got {value!r}")
    return int(value)


_validate_default_branch = validate_default_branch


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


def _validate_rigor_override(value: Any) -> Optional[str]:
    """Validate a per-repo ``default_rigor``; returns None when the override should be cleared."""
    return _validate_design_override("default_rigor", value, RIGOR_LEVELS)


def _validate_editable_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Validate supplied editable fields; returns normalized values (None = clear override)."""
    validated: dict[str, Any] = {}
    for field, value in fields.items():
        if field == "merge_policy":
            validated[field] = _validate_merge_policy(value)
        elif field == "publish_policy":
            validated[field] = _validate_publish_policy(value)
        elif field == "max_fix_rounds":
            validated[field] = _validate_max_fix_rounds(value)
        elif field == "default_branch":
            validated[field] = _validate_default_branch(value)
        elif field == "design_provider":
            validated[field] = _validate_design_override(field, value, DESIGN_PROVIDERS)
        elif field == "design_approval":
            validated[field] = _validate_design_override(field, value, DESIGN_APPROVAL_POLICIES)
        elif field == "default_rigor":
            validated[field] = _validate_rigor_override(value)
        else:
            raise ValueError(f"Unknown editable repository field {field!r}")
    return validated


def _run_gh(args: list[str], *, cwd: Optional[Path] = None, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    """`gh` seam: every repos call goes through this name (patched by tests as ``factonio.repos._run_gh``)."""
    return run_gh(args, cwd=cwd, timeout=timeout)


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
    publish_policy: Any = _UNSET,
) -> dict[str, Any]:
    """Register a GitHub repository with Factonio and anchor a Hermes Project.

    Re-running on an already-registered repository preserves existing per-repo overrides
    (merge policy, publish policy, fix rounds, default branch, design settings, CI contract, models)
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

    # Omitted inherits (missing key -> global default); an explicit value, including None, must be
    # valid, and a persisted null/"" fails closed here.
    if publish_policy is _UNSET:
        publish = resolve_publish_policy(repo=existing)
    else:
        publish = _validate_publish_policy(publish_policy)

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
        if publish_policy is _UNSET and "publish_policy" in existing:
            publish = resolve_publish_policy(repo=existing)
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
            publish_policy=publish,
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
    publish_policy: str = "auto",
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
        "publish_policy": publish_policy,
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
    default_rigor: Any = _UNSET,
    publish_policy: Any = _UNSET,
) -> dict[str, Any]:
    """Update editable parameters of a registered repository (only fields explicitly supplied).

    All supplied values are validated before anything is written (all-or-nothing). The full
    entry (including fields that only lived in config.yaml) is persisted atomically to
    ``plugin-data/factonio/repos.json``. Passing ``None``/``""``/``"inherit"``/``"default"`` for
    ``design_provider``, ``design_approval`` or ``default_rigor`` removes the per-repo override so
    the global default applies.
    """
    found = get_repo(slug_or_full)
    if found is None:
        raise RepoNotFoundError(f"Repository {slug_or_full!r} is not registered.")

    supplied = {
        "merge_policy": merge_policy,
        "publish_policy": publish_policy,
        "max_fix_rounds": max_fix_rounds,
        "default_branch": default_branch,
        "design_provider": design_provider,
        "design_approval": design_approval,
        "default_rigor": default_rigor,
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
            if field in _CLEARABLE_REPO_FIELDS and value is None:
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


_PR_GATE_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $threadsCursor: String, $contextsCursor: String) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef { name }
    pullRequest(number: $number) {
      number
      url
      title
      state
      isDraft
      headRefName
      headRefOid
      baseRefName
      mergeable
      mergeStateStatus
      reviewDecision
      reviewThreads(first: 100, after: $threadsCursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          path
          line
          originalLine
          comments(first: 1) { nodes { databaseId author { login __typename } body createdAt } }
        }
      }
      commits(last: 1) {
        nodes {
          commit {
            oid
            statusCheckRollup {
              state
              contexts(first: 100, after: $contextsCursor) {
                pageInfo { hasNextPage endCursor }
                nodes {
                  __typename
                  ... on CheckRun { name status conclusion }
                  ... on StatusContext { context state }
                }
              }
            }
          }
        }
      }
      commitHistory: commits(last: 50) { nodes { commit { oid statusCheckRollup { state } } } }
    }
  }
}
"""

_PR_GATE_MAX_PAGES = 20
_CHECK_PASS = frozenset({"SUCCESS", "SKIPPED", "NEUTRAL"})
_CHECK_FAIL = frozenset({"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "STARTUP_FAILURE"})
_STATUS_FAIL = frozenset({"FAILURE", "ERROR"})
_ROLLUP_FAIL = frozenset({"FAILURE", "ERROR"})
_THREAD_SNIPPET_MAX = 200
# Owner-approval gates are human (Tier 4) gates, never Tier 3 CI failures.
_APPROVAL_GATE_CONCLUSIONS = frozenset({"ACTION_REQUIRED"})
_APPROVAL_GATE_TOKENS = ("code review gate", "owner approval", "manual approval")
_REVIEW_AUTOMATION_TOKENS = ("bugbot", "security review", "pr review automation", "review automation")
_PENDING_STATUSES = frozenset({"QUEUED", "IN_PROGRESS", "PENDING", "WAITING", "REQUESTED"})
_KNOWN_REVIEW_BOTS = frozenset({
    "cursor", "bugbot", "cursor-bugbot", "coderabbitai", "copilot", "copilot-pull-request-reviewer",
    "greptile-apps", "sourcery-ai", "github-actions", "dependabot", "renovate", "sonarcloud", "qodo-merge-pro",
})

_GRAPHQL_MAX_ATTEMPTS = 3
_GRAPHQL_BASE_DELAY_S = 0.25
_GRAPHQL_MAX_DELAY_S = 4.0
_graphql_sleep = time.sleep

_GRAPHQL_RATE_LIMIT_MARKERS = (
    "rate limit", "rate-limit", "ratelimit", "rate_limited", "http 429", "too many requests", "abuse detection",
)
_GRAPHQL_FATAL_MARKERS = (
    "not found", "could not resolve", "http 401", "http 403", "bad credentials", "no auth", "not logged",
    "gh auth login", "authentication", "unauthorized", "forbidden", "resource not accessible",
    "doesn't exist on type", "does not exist on type", "parse error", "syntax error", "argument",
    "variable", "undefinedfield", "validation",
)
_GRAPHQL_TRANSIENT_MARKERS = (
    "http 500", "http 502", "http 503", "http 504", "bad gateway", "service unavailable", "gateway timeout",
    "internal server error", "timeout", "timed out", "deadline exceeded", "connection", "network", "eof",
    "tls", "handshake", "temporarily", "try again", "something went wrong", "invalid json",
    "unexpected end", "broken pipe", "no such host", "dial tcp", "i/o", "reset by peer", "unreachable",
)


class _GateQueryError(RuntimeError):
    """A `gh api graphql` PR gate query failed (exit code, JSON, GraphQL errors, or missing PR)."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


def _is_retryable_graphql_error(message: Any) -> bool:
    """True for transient transport/5xx/rate-limit/invalid-JSON failures; False for not-found/auth/validation."""
    text = str(message or "").lower()
    if not text:
        return False
    if any(m in text for m in _GRAPHQL_RATE_LIMIT_MARKERS):
        return True
    if any(m in text for m in _GRAPHQL_FATAL_MARKERS):
        return False
    return any(m in text for m in _GRAPHQL_TRANSIENT_MARKERS)


def _graphql_backoff_delay(attempt: int) -> float:
    return min(_GRAPHQL_MAX_DELAY_S, _GRAPHQL_BASE_DELAY_S * (2 ** max(0, attempt - 1)))


def _gate_graphql_once(full_repo: str, pr_number: int, cursors: dict[str, Optional[str]]) -> dict[str, Any]:
    owner, name = full_repo.split("/", 1)
    args = [
        "api", "graphql",
        "-f", f"query={_PR_GATE_QUERY}",
        "-f", f"owner={owner}",
        "-f", f"name={name}",
        "-F", f"number={int(pr_number)}",
    ]
    for key, value in cursors.items():
        if value:
            args.extend(["-f", f"{key}={value}"])
    try:
        proc = _run_gh(args)
    except FileNotFoundError as exc:
        raise _GateQueryError(f"gh api graphql failed: {exc}") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise _GateQueryError(f"gh api graphql failed: {exc}", retryable=True) from exc
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout or f"gh exit {proc.returncode}").strip()
        raise _GateQueryError(message, retryable=_is_retryable_graphql_error(message))
    try:
        payload = json.loads(proc.stdout or "")
    except ValueError as exc:
        raise _GateQueryError(f"gh api graphql returned invalid JSON: {exc}", retryable=True) from exc
    if not isinstance(payload, dict):
        raise _GateQueryError("gh api graphql returned an unexpected payload", retryable=True)
    errors = payload.get("errors")
    if errors:
        msgs = [str(e.get("message") or e) if isinstance(e, dict) else str(e) for e in errors]
        types = " ".join(str(e.get("type") or "") for e in errors if isinstance(e, dict)).lower()
        message = "GraphQL error: " + "; ".join(msgs)
        retryable = "rate_limited" in types or any(m in message.lower() for m in _GRAPHQL_RATE_LIMIT_MARKERS)
        raise _GateQueryError(message, retryable=retryable)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    repo_node = data.get("repository") if isinstance(data.get("repository"), dict) else {}
    pr = repo_node.get("pullRequest")
    if not isinstance(pr, dict):
        raise _GateQueryError(f"Pull request #{int(pr_number)} not found in {full_repo}")
    default_ref = repo_node.get("defaultBranchRef")
    if isinstance(default_ref, dict) and default_ref.get("name"):
        pr = {**pr, "_default_branch": str(default_ref["name"])}
    return pr


def _gate_graphql(
    full_repo: str,
    pr_number: int,
    *,
    max_attempts: Optional[int] = None,
    sleep_fn: Optional[Any] = None,
    **cursors: Optional[str],
) -> dict[str, Any]:
    """Run one PR gate GraphQL page and return the `pullRequest` node.

    Retryable failures (transport/timeouts, 429/5xx/rate limits, invalid JSON) are retried with capped
    exponential backoff (``_GRAPHQL_BASE_DELAY_S`` doubling up to ``_GRAPHQL_MAX_DELAY_S``) for at most
    ``max_attempts`` (default ``_GRAPHQL_MAX_ATTEMPTS``) attempts; not-found, auth and GraphQL validation
    errors fail fast on the first attempt.
    """
    attempts = max(1, int(max_attempts if max_attempts is not None else _GRAPHQL_MAX_ATTEMPTS))
    sleep = sleep_fn if sleep_fn is not None else _graphql_sleep
    attempt = 1
    while True:
        try:
            return _gate_graphql_once(full_repo, pr_number, cursors)
        except _GateQueryError as exc:
            if not exc.retryable or attempt >= attempts:
                raise
            sleep(_graphql_backoff_delay(attempt))
            attempt += 1


def _connection(node: Any) -> tuple[list[Any], bool, Optional[str]]:
    if not isinstance(node, dict):
        return [], False, None
    nodes = [n for n in (node.get("nodes") or []) if n is not None]
    info = node.get("pageInfo") if isinstance(node.get("pageInfo"), dict) else {}
    cursor = info.get("endCursor")
    return nodes, bool(info.get("hasNextPage")), (str(cursor) if cursor else None)


def _head_rollup(pr: dict[str, Any]) -> Optional[dict[str, Any]]:
    commits = pr.get("commits") if isinstance(pr.get("commits"), dict) else {}
    nodes = [n for n in (commits.get("nodes") or []) if isinstance(n, dict)]
    if not nodes:
        return None
    commit = nodes[-1].get("commit") if isinstance(nodes[-1].get("commit"), dict) else {}
    rollup = commit.get("statusCheckRollup")
    return rollup if isinstance(rollup, dict) else None


def _had_previous_passing_ci(pr: dict[str, Any]) -> bool:
    """True iff any PR commit before the head had a ``SUCCESS`` status-check rollup (flake signal)."""
    history = pr.get("commitHistory") if isinstance(pr.get("commitHistory"), dict) else None
    if history is None:
        history = pr.get("commits") if isinstance(pr.get("commits"), dict) else {}
    nodes = [n for n in (history.get("nodes") or []) if isinstance(n, dict)]
    head_oid = pr.get("headRefOid")
    for node in nodes[:-1]:
        commit = node.get("commit") if isinstance(node.get("commit"), dict) else {}
        if head_oid and commit.get("oid") == head_oid:
            continue
        rollup = commit.get("statusCheckRollup") if isinstance(commit.get("statusCheckRollup"), dict) else {}
        if str(rollup.get("state") or "").upper() == "SUCCESS":
            return True
    return False


def _thread_snippet(body: Any) -> str:
    text = " ".join(str(body or "").split())
    if len(text) > _THREAD_SNIPPET_MAX:
        text = text[: _THREAD_SNIPPET_MAX - 3].rstrip() + "..."
    return text


def _is_review_bot(author: Any) -> bool:
    if not isinstance(author, dict):
        return False
    if str(author.get("__typename") or "") == "Bot":
        return True
    login = str(author.get("login") or "").strip().lower()
    if not login:
        return False
    return login.endswith("[bot]") or login.endswith("-bot") or login in _KNOWN_REVIEW_BOTS


def _comment_id(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _normalize_thread(node: dict[str, Any]) -> dict[str, Any]:
    comments = node.get("comments") if isinstance(node.get("comments"), dict) else {}
    first = next((c for c in (comments.get("nodes") or []) if isinstance(c, dict)), None)
    author = first.get("author") if isinstance(first, dict) else None
    return {
        "id": str(node.get("id") or ""),
        "comment_id": _comment_id(first.get("databaseId")) if isinstance(first, dict) else None,
        "path": node.get("path"),
        "line": node.get("line") if node.get("line") is not None else node.get("originalLine"),
        "author": author.get("login") if isinstance(author, dict) else None,
        "is_review_bot": _is_review_bot(author),
        "body": _thread_snippet(first.get("body")) if isinstance(first, dict) else "",
        "created_at": first.get("createdAt") if isinstance(first, dict) else None,
    }


def _name_matches(name: str, tokens: tuple[str, ...]) -> bool:
    lowered = name.lower()
    return any(token in lowered for token in tokens)


def _classify_contexts(
    contexts: list[Any],
) -> tuple[list[str], list[str], list[str], list[str], list[str]]:
    """Split check contexts into ``(passed, failed, pending, approval_gates, pending_review_automations)``.

    Passing checks always land in ``passed``. Non-passing owner-approval checks (``ACTION_REQUIRED`` or an
    approval-token name) land only in ``approval_gates``. Pending review-automation checks stay in
    ``pending`` and are also listed in ``pending_review_automations``.
    """
    passed: list[str] = []
    failed: list[str] = []
    pending: list[str] = []
    approval_gates: list[str] = []
    automations: list[str] = []

    def _pending(name: str, label: str) -> None:
        pending.append(label)
        if _name_matches(name, _REVIEW_AUTOMATION_TOKENS):
            automations.append(name)

    for item in contexts:
        if not isinstance(item, dict):
            continue
        kind = item.get("__typename")
        if kind == "StatusContext" or (kind is None and "context" in item and "conclusion" not in item):
            name = str(item.get("context") or item.get("name") or "unnamed")
            state = str(item.get("state") or "").upper()
            if state == "SUCCESS":
                passed.append(name)
            elif _name_matches(name, _APPROVAL_GATE_TOKENS):
                approval_gates.append(name)
            elif state in _STATUS_FAIL:
                failed.append(f"{name} ({state.lower()})")
            else:
                _pending(name, name)
            continue
        name = str(item.get("name") or item.get("context") or "unnamed")
        status = str(item.get("status") or "").upper()
        conclusion = str(item.get("conclusion") or "").upper()
        if conclusion in _CHECK_PASS:
            passed.append(name)
        elif conclusion in _APPROVAL_GATE_CONCLUSIONS or _name_matches(name, _APPROVAL_GATE_TOKENS):
            approval_gates.append(name)
        elif conclusion in _CHECK_FAIL:
            failed.append(f"{name} ({conclusion.lower()})")
        elif not conclusion or status in _PENDING_STATUSES:
            _pending(name, name)
        else:
            _pending(name, f"{name} ({conclusion.lower()})")
    return passed, failed, pending, approval_gates, automations


def _fetch_gate_refs(
    cwd: Optional[Path],
    *,
    head_sha: Optional[str],
    head_branch: Optional[str],
    base_branch: str,
    pr_number: int,
    remote: str,
) -> None:
    """Best-effort fetch of ``<remote>/<base>`` and the PR head commit for local gate checks.

    The head branch is tried first, but a fork PR's ``headRefName`` may collide with an unrelated
    branch on ``remote`` (e.g. ``main``), so ``refs/pull/<n>/head`` is fetched whenever the exact
    head commit is still missing afterwards.
    """
    if cwd is None:
        return
    try:
        _fetch_branch(cwd, remote, _validate_default_branch(base_branch))
        if not head_sha:
            return
        head = _validate_git_ref(str(head_sha), "head_sha")
        if _verify_commit(cwd, head):
            return
        if head_branch:
            try:
                _fetch_branch(cwd, remote, _validate_default_branch(str(head_branch)))
            except ValueError:
                pass
        if not _verify_commit(cwd, head):
            _fetch_pull_head(cwd, remote, pr_number)
    except (ValueError, OSError, subprocess.SubprocessError):
        return


def _local_trunk_drift(
    cwd: Optional[Path],
    *,
    head_sha: Optional[str],
    base_branch: str,
    default_branch: str,
    remote: str,
) -> Optional[dict[str, Any]]:
    """Run `assess_trunk_drift` only when the checkout resolves both `<remote>/<base>` and the PR head."""
    if cwd is None or not head_sha:
        return None
    try:
        head = _validate_git_ref(str(head_sha), "head_sha")
        base = _validate_default_branch(base_branch)
        if not _verify_commit(cwd, f"refs/remotes/{remote}/{base}") or not _verify_commit(cwd, head):
            return None
    except (ValueError, OSError, subprocess.SubprocessError):
        return None
    return assess_trunk_drift(cwd, head, base_branch=base, default_branch=default_branch, remote=remote)


def _gate_patch_continuity(
    cwd: Optional[Path],
    verified_head_sha: str,
    head_sha: Optional[str],
    *,
    base_branch: str,
    remote: str,
) -> dict[str, Any]:
    if head_sha and verified_head_sha == head_sha:
        return {
            "ok": True,
            "continuous": True,
            "same_commit": True,
            "verified_sha": verified_head_sha,
            "head_sha": head_sha,
            "reason": "same commit",
        }
    unavailable = {"ok": False, "continuous": False, "verified_sha": verified_head_sha, "head_sha": head_sha}
    if cwd is None:
        return {**unavailable, "error": "local checkout not registered or missing"}
    if not head_sha:
        return {**unavailable, "error": "live PR head_sha unavailable"}
    try:
        if not _verify_commit(cwd, _validate_git_ref(verified_head_sha, "verified_head_sha")):
            return {**unavailable, "error": f"verified commit {verified_head_sha} is not available locally"}
        if not _verify_commit(cwd, _validate_git_ref(str(head_sha), "head_sha")):
            return {**unavailable, "error": f"PR head {head_sha} is not available locally (try fetch=True)"}
        base_ref = f"{remote}/{_validate_default_branch(base_branch)}"
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        return {**unavailable, "error": str(exc)}
    return verify_patch_continuity(cwd, verified_head_sha, str(head_sha), base_ref=base_ref)


def inspect_pr_gate(
    repo_spec: str,
    pr_number: int,
    *,
    verified_head_sha: Optional[str] = None,
    check_trunk_drift: bool = True,
    fetch: bool = False,
    remote: str = "origin",
) -> dict[str, Any]:
    """Inspect a GitHub PR's exact HEAD, merge state, review threads, and CI checks via GraphQL.

    Uses ``gh api graphql`` with cursor pagination for review threads and head-commit
    check contexts (each capped at 20 pages of 100). Blockers are classified in strict
    priority tiers: 1 merge conflicts / rebase-required trunk drift, 2 unresolved review
    threads, 3 failing/pending CI, 4 merge gates (closed/merged, draft, changes requested,
    owner-approval gates).

    ``trunk_drift`` is computed only when a registered local checkout resolves both
    ``<remote>/<base_branch>`` and the live head (``fetch=True`` fetches them first);
    otherwise it is ``None``. ``patch_continuity`` is computed when ``verified_head_sha``
    is supplied.
    """
    remote = _validate_remote(remote)
    repo_entry = get_repo(repo_spec)
    full_repo = str(repo_entry["repo"]) if repo_entry else normalize_repo(repo_spec)[0]

    def _error(message: str) -> dict[str, Any]:
        return {"ok": False, "repo": full_repo, "pr_number": int(pr_number), "error": message}

    try:
        data = _gate_graphql(full_repo, pr_number)
        threads, threads_more, threads_cursor = _connection(data.get("reviewThreads"))
        pages = 1
        while threads_more and threads_cursor and pages < _PR_GATE_MAX_PAGES:
            page = _gate_graphql(full_repo, pr_number, threadsCursor=threads_cursor)
            nodes, threads_more, threads_cursor = _connection(page.get("reviewThreads"))
            threads.extend(nodes)
            pages += 1
        threads_truncated = bool(threads_more and threads_cursor)

        rollup = _head_rollup(data)
        contexts, ctx_more, ctx_cursor = _connection((rollup or {}).get("contexts"))
        pages = 1
        while ctx_more and ctx_cursor and pages < _PR_GATE_MAX_PAGES:
            page = _gate_graphql(full_repo, pr_number, contextsCursor=ctx_cursor)
            nodes, ctx_more, ctx_cursor = _connection((_head_rollup(page) or {}).get("contexts"))
            contexts.extend(nodes)
            pages += 1
        contexts_truncated = bool(ctx_more and ctx_cursor)
    except _GateQueryError as exc:
        return _error(str(exc))
    except (OSError, subprocess.SubprocessError) as exc:
        return _error(f"gh api graphql failed: {exc}")

    unresolved_threads = [
        _normalize_thread(n) for n in threads if isinstance(n, dict) and not n.get("isResolved")
    ]
    passed, failed, pending, approval_gates, pending_automations = _classify_contexts(contexts)
    head_rollup_state = str((rollup or {}).get("state") or "").upper() or None

    state = data.get("state")
    state_u = str(state or "").upper()
    is_draft = bool(data.get("isDraft"))
    mergeable_u = str(data.get("mergeable") or "").upper()
    merge_state_u = str(data.get("mergeStateStatus") or "").upper()
    review_decision = data.get("reviewDecision") or None
    review_decision_u = str(review_decision or "").upper()
    head_sha = data.get("headRefOid")
    base_branch = data.get("baseRefName")
    default_branch = str(data.get("_default_branch") or (repo_entry or {}).get("default_branch") or "main")
    true_base = str(base_branch or default_branch)

    _, cwd = _registered_checkout(repo_spec) if repo_entry else (None, None)
    want_drift = check_trunk_drift and state_u == "OPEN"
    if fetch and (want_drift or verified_head_sha):
        _fetch_gate_refs(
            cwd,
            head_sha=head_sha,
            head_branch=data.get("headRefName"),
            base_branch=true_base,
            pr_number=int(pr_number),
            remote=remote,
        )
    trunk_drift: Optional[dict[str, Any]] = None
    if want_drift:
        trunk_drift = _local_trunk_drift(
            cwd, head_sha=head_sha, base_branch=true_base, default_branch=default_branch, remote=remote,
        )
    patch_continuity: Optional[dict[str, Any]] = None
    if verified_head_sha:
        patch_continuity = _gate_patch_continuity(
            cwd, str(verified_head_sha), head_sha, base_branch=true_base, remote=remote,
        )

    # A BLOCKED/failing rollup is the approval gate's doing when every actual CI check is clean.
    rollup_from_approval = bool(approval_gates) and not failed and not pending and not contexts_truncated

    tiers: dict[int, list[str]] = {1: [], 2: [], 3: [], 4: []}
    if mergeable_u == "CONFLICTING" or merge_state_u in {"DIRTY", "CONFLICTING"}:
        tiers[1].append("PR has merge conflicts")
    if trunk_drift and trunk_drift.get("ok") and trunk_drift.get("rebase_required"):
        files = trunk_drift.get("workflow_files") or trunk_drift.get("overlapping_files") or []
        tiers[1].append(
            f"Trunk drift requires rebase ({trunk_drift.get('policy')}): "
            + (", ".join(files[:10]) if files else f"{trunk_drift.get('trunk_ref')} moved")
        )
    if unresolved_threads:
        tiers[2].append(f"Unresolved review threads ({len(unresolved_threads)})")
    if threads_truncated:
        tiers[2].append(f"Review threads exceed {_PR_GATE_MAX_PAGES} pages; unresolved count may be incomplete")
    if failed:
        tiers[3].append("Failed checks: " + ", ".join(failed))
    if pending:
        tiers[3].append("Pending checks: " + ", ".join(pending))
    if contexts_truncated:
        tiers[3].append(f"Check contexts exceed {_PR_GATE_MAX_PAGES} pages; check status is incomplete")
    if merge_state_u == "BLOCKED" and head_rollup_state in _ROLLUP_FAIL and not rollup_from_approval:
        tiers[3].append(f"Merge blocked with head statusCheckRollup={head_rollup_state}")
    if state_u != "OPEN":
        tiers[4].append(f"PR state is {state}")
    if is_draft:
        tiers[4].append("PR is still a draft")
    if review_decision_u == "CHANGES_REQUESTED":
        tiers[4].append("Review decision is CHANGES_REQUESTED")
    if approval_gates:
        tiers[4].append("Approval gate required: " + ", ".join(approval_gates))

    blockers = [b for tier in (1, 2, 3, 4) for b in tiers[tier]]
    blocker_tier = next((tier for tier in (1, 2, 3, 4) if tiers[tier]), None)
    checks_green = (
        not failed
        and not pending
        and not contexts_truncated
        and (head_rollup_state not in _ROLLUP_FAIL or rollup_from_approval)
    )
    return {
        "ok": True,
        "repo": full_repo,
        "pr_number": int(data.get("number") or pr_number),
        "url": data.get("url"),
        "title": data.get("title"),
        "state": state,
        "is_draft": is_draft,
        "head_branch": data.get("headRefName"),
        "head_sha": head_sha,
        "base_branch": base_branch,
        "mergeable": data.get("mergeable"),
        "merge_state_status": data.get("mergeStateStatus"),
        "checks": {"passed": passed, "failed": failed, "pending": pending},
        "checks_green": checks_green,
        "ready_to_merge": not blockers,
        "blockers": blockers,
        "review_decision": review_decision,
        "unresolved_threads": unresolved_threads,
        "blocker_tier": blocker_tier,
        "head_rollup_state": head_rollup_state,
        "default_branch": default_branch,
        "is_stacked": bool(base_branch and base_branch != default_branch),
        "approval_gates": approval_gates,
        "review_automation_running": bool(pending_automations),
        "pending_review_automations": pending_automations,
        "had_previous_passing_ci": _had_previous_passing_ci(data),
        "trunk_drift": trunk_drift,
        "patch_continuity": patch_continuity,
        "blocker_tiers": {str(tier): list(tiers[tier]) for tier in (1, 2, 3, 4)},
    }


_STACK_TIER_ORDER = ("tier_1", "tier_2", "tier_3_failed", "tier_4", "tier_3_pending")
_STACK_TIER_NUMBER = {"tier_1": 1, "tier_2": 2, "tier_3_failed": 3, "tier_4": 4, "tier_3_pending": 3}
_STACK_NEXT_ACTION = {
    "tier_1": "resolve_conflicts_or_rebase",
    "tier_2": "triage_review_threads",
    "tier_3_failed": "fix_or_rerun_failed_ci",
    "tier_4": "human_gate",
    "tier_3_pending": "wait_for_pending_checks",
}


def _order_stack(gates: dict[int, dict[str, Any]], numbers: list[int]) -> tuple[list[int], list[str]]:
    """Order PRs root-to-tip via ``base_branch -> head_branch`` links; cycle-safe (never loops)."""
    warnings: list[str] = []
    by_head: dict[str, int] = {}
    for n in numbers:
        head = gates[n].get("head_branch")
        if head:
            if head in by_head:
                warnings.append(f"PRs #{by_head[head]} and #{n} share head branch {head!r}")
            by_head.setdefault(str(head), n)
    parent: dict[int, Optional[int]] = {}
    for n in numbers:
        p = by_head.get(str(gates[n].get("base_branch") or ""))
        parent[n] = p if p is not None and p != n else None
    children: dict[int, list[int]] = {n: [] for n in numbers}
    for n in numbers:
        if parent[n] is not None:
            children[parent[n]].append(n)
    order: list[int] = []
    seen: set[int] = set()
    for root in (n for n in numbers if parent[n] is None):
        stack = [root]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            order.append(cur)
            kids = [k for k in children[cur] if k not in seen]
            if len(kids) > 1:
                warnings.append(f"PR #{cur} has multiple stacked children {kids}; stack is a tree")
            stack.extend(reversed(kids))
    leftover = [n for n in numbers if n not in seen]
    if leftover:
        warnings.append(f"Stack cycle detected among PRs {leftover}; appended in input order")
        order.extend(leftover)
    return order, warnings


def inspect_stack_gate(
    repo_spec: str,
    pr_numbers: Any,
    *,
    check_trunk_drift: bool = True,
    fetch: bool = False,
    remote: str = "origin",
) -> dict[str, Any]:
    """Gate a whole PR stack bottom-to-top in tier-major order.

    Tier order across all PRs: Tier 1 conflicts/drift -> Tier 2 unresolved threads -> Tier 3 CI failures
    -> Tier 4 human gates -> lowest pending frontier. ``contiguous_ready_run`` is the longest ready prefix
    from the root, ``ceiling`` the first PR above it that blocks, and ``ready_to_merge`` is True only when
    every PR in an acyclic stack is ready.
    """
    remote = _validate_remote(remote)
    if isinstance(pr_numbers, (str, bytes)) or not isinstance(pr_numbers, (list, tuple)):
        raise ValueError(f"pr_numbers must be a non-empty list of positive integers; got {pr_numbers!r}")
    numbers: list[int] = []
    for value in pr_numbers:
        n = _validate_pr_number(value, "pr_numbers")
        if n not in numbers:
            numbers.append(n)
    if not numbers:
        raise ValueError("pr_numbers must be a non-empty list of positive integers")

    gates = {
        n: inspect_pr_gate(repo_spec, n, check_trunk_drift=check_trunk_drift, fetch=fetch, remote=remote)
        for n in numbers
    }
    repo_entry = get_repo(repo_spec)
    full_repo = str(repo_entry["repo"]) if repo_entry else normalize_repo(repo_spec)[0]
    errors = {n: str(g.get("error") or "gate inspection failed") for n, g in gates.items() if not g.get("ok")}
    if errors:
        return {
            "ok": False,
            "repo": full_repo,
            "pr_numbers": numbers,
            "error": "; ".join(f"PR #{n}: {msg}" for n, msg in errors.items()),
            "errors": {str(n): msg for n, msg in errors.items()},
            "ready_to_merge": False,
            "prs": [gates[n] for n in numbers],
        }

    order, warnings = _order_stack(gates, numbers)
    cycle = any(w.startswith("Stack cycle") for w in warnings)
    tiers_summary: dict[str, list[dict[str, Any]]] = {key: [] for key in _STACK_TIER_ORDER}
    prs: list[dict[str, Any]] = []
    for position, n in enumerate(order):
        gate = gates[n]
        bt = gate.get("blocker_tiers") or {}
        checks = gate.get("checks") or {}
        tier3_failed = [b for b in (bt.get("3") or []) if not str(b).startswith("Pending checks:")]
        tier3_pending = [b for b in (bt.get("3") or []) if str(b).startswith("Pending checks:")]
        if gate.get("review_automation_running") and not tier3_pending:
            tier3_pending = ["Review automation running: " + ", ".join(gate.get("pending_review_automations") or [])]
        buckets = {
            "tier_1": list(bt.get("1") or []),
            "tier_2": list(bt.get("2") or []),
            "tier_3_failed": tier3_failed,
            "tier_4": list(bt.get("4") or []),
            "tier_3_pending": tier3_pending,
        }
        for key, items in buckets.items():
            if items:
                tiers_summary[key].append({"pr_number": n, "position": position, "blockers": items})
        prs.append({
            "pr_number": n,
            "position": position,
            "url": gate.get("url"),
            "head_branch": gate.get("head_branch"),
            "base_branch": gate.get("base_branch"),
            "head_sha": gate.get("head_sha"),
            "is_stacked": gate.get("is_stacked"),
            "ready_to_merge": bool(gate.get("ready_to_merge")),
            "blocker_tier": gate.get("blocker_tier"),
            "blockers": list(gate.get("blockers") or []),
            "pending_checks": list(checks.get("pending") or []),
            "review_automation_running": bool(gate.get("review_automation_running")),
            "pending_review_automations": list(gate.get("pending_review_automations") or []),
            "approval_gates": list(gate.get("approval_gates") or []),
            "trunk_drift_policy": (gate.get("trunk_drift") or {}).get("policy"),
        })

    run: list[int] = []
    for entry in prs:
        if not entry["ready_to_merge"]:
            break
        run.append(entry["pr_number"])
    ceiling: Optional[dict[str, Any]] = None
    if len(run) < len(prs):
        top = prs[len(run)]
        ceiling = {k: top[k] for k in ("pr_number", "blocker_tier", "blockers", "head_branch", "base_branch", "head_sha")}
    frontier: Optional[dict[str, Any]] = None
    pending_entry = next((e for e in prs if e["pending_checks"] or e["review_automation_running"]), None)
    if pending_entry is not None:
        frontier = {
            k: pending_entry[k]
            for k in (
                "pr_number", "pending_checks", "pending_review_automations", "review_automation_running",
                "head_branch", "base_branch", "head_sha",
            )
        }
    active = [key for key in _STACK_TIER_ORDER if tiers_summary[key]]
    highest = min((_STACK_TIER_NUMBER[key] for key in active), default=None)
    ready = len(run) == len(prs) and not cycle
    if active:
        key = active[0]
        first = tiers_summary[key][0]
        next_action: dict[str, Any] = {
            "action": _STACK_NEXT_ACTION[key],
            "tier": key,
            "pr_number": first["pr_number"],
            "blockers": first["blockers"],
        }
    elif cycle:
        next_action = {"action": "fix_stack_cycle", "tier": None, "pr_number": None, "blockers": warnings}
    else:
        next_action = {"action": "merge_bottom", "tier": None, "pr_number": run[0] if run else None, "blockers": []}
    return {
        "ok": True,
        "repo": full_repo,
        "pr_numbers": numbers,
        "order": order,
        "cycle_detected": cycle,
        "warnings": warnings,
        "prs": prs,
        "tiers_summary": tiers_summary,
        "lowest_pending_frontier": frontier,
        "contiguous_ready_run": run,
        "ceiling": ceiling,
        "highest_blocker_tier": highest,
        "next_action": next_action,
        "ready_to_merge": ready,
        "gates": {str(n): gates[n] for n in order},
    }


# ---------------------------------------------------------------------------
# Pure-Python git helpers (stdlib + `git` CLI only)
# ---------------------------------------------------------------------------


def _run_git(
    args: list[str],
    *,
    cwd: Path,
    input_text: Optional[str] = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    """`git` seam: every repos call goes through this name (patched by tests as ``factonio.repos._run_git``)."""
    return run_git(args, cwd=cwd, input_text=input_text, timeout=timeout)


def _run_git_ro(args: list[str], *, cwd: Path, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    """Read-only git probe: never refresh/rewrite the index or take optional locks."""
    return _run_git(["--no-optional-locks", *args], cwd=cwd, timeout=timeout)


_git_err = git_err


def _validate_git_ref(value: Any, field: str = "ref") -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty git revision; got {value!r}")
    ref = value.strip()
    if ref.startswith("-"):
        raise ValueError(f"{field} must not start with '-'; got {value!r}")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in ref):
        raise ValueError(f"{field} must not contain whitespace or control characters; got {value!r}")
    return ref


def _validate_remote(value: Any) -> str:
    remote = _validate_git_ref(value, "remote")
    if any(ch in remote for ch in "/\\:") or ".." in remote:
        raise ValueError(f"remote must be a plain remote name; got {value!r}")
    return remote


def _is_configured_remote(cwd: Path, remote: str) -> bool:
    """True only when ``remote`` is a named remote in ``cwd``'s git config (not a path or URL)."""
    # Exact line membership: a configured name containing spaces must not tokenize into others.
    proc = _run_git(["remote"], cwd=cwd)
    if proc.returncode != 0 or remote not in (proc.stdout or "").splitlines():
        return False
    return _run_git(["remote", "get-url", "--", remote], cwd=cwd).returncode == 0


def _sync_local_default_branch(repo_path: Path, branch: str) -> tuple[bool, str]:
    """Fast-forward the local `branch` to `origin/<branch>` without disturbing any checkout.

    - Checked out on `branch` with a clean tree: `git fetch` + `git merge --ff-only`.
    - Checked out on `branch` with a dirty tree: skipped.
    - Any other branch / detached HEAD: strict fast-forward refspec `origin <branch>:<branch>`
      (git refuses non-fast-forwards and branches checked out in any worktree).
    Never raises: returns ``(synced, note)``.
    """
    try:
        branch = _validate_default_branch(branch)
        repo_path = Path(repo_path)
        head = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_path)
        if head.returncode != 0:
            return False, "skipped: " + _git_err(head, "rev-parse")
        current = head.stdout.strip()
        if current == branch:
            status = _run_git(["status", "--porcelain"], cwd=repo_path)
            if status.returncode != 0:
                return False, "skipped: " + _git_err(status, "status")
            if status.stdout.strip():
                return False, f"skipped: working tree on '{branch}' is dirty"
            fetch = _run_git(["fetch", "origin", branch], cwd=repo_path, timeout=120)
            if fetch.returncode != 0:
                return False, "skipped: " + _git_err(fetch, "fetch")
            ff = _run_git(["merge", "--ff-only", f"origin/{branch}"], cwd=repo_path, timeout=60)
            if ff.returncode != 0:
                return False, "skipped: " + _git_err(ff, "merge --ff-only")
            return True, f"fast-forwarded checked-out '{branch}' to origin/{branch}"
        fetch = _run_git(["fetch", "origin", branch], cwd=repo_path, timeout=120)
        if fetch.returncode != 0:
            return False, "skipped: " + _git_err(fetch, "fetch")
        upd = _run_git(["fetch", "origin", f"{branch}:{branch}"], cwd=repo_path, timeout=120)
        if upd.returncode != 0:
            return False, "skipped: " + _git_err(upd, "fetch (fast-forward refspec)")
        return True, f"fast-forwarded local '{branch}' to origin/{branch} (checkout on '{current}' untouched)"
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        return False, f"skipped: {exc}"


def compute_patch_id(repo_path: str | Path, rev: str, *, base_ref: Optional[str] = None) -> Optional[str]:
    """Return the stable `git patch-id` of a commit, range, or merge-base diff (None if empty).

    - ``base_ref`` given: ``git diff <base_ref>...<rev>`` (all PR commits since the merge-base).
    - ``rev`` contains ``..``: ``git diff <rev>``.
    - Otherwise: ``git show --format= --patch <rev>``.
    Raises ``ValueError`` for unsafe refs and ``RuntimeError`` on git failures.
    """
    cwd = Path(repo_path)
    rev = _validate_git_ref(rev, "rev")
    if base_ref is not None:
        base = _validate_git_ref(base_ref, "base_ref")
        diff_args = ["diff", f"{base}...{rev}"]
    elif ".." in rev:
        diff_args = ["diff", rev]
    else:
        diff_args = ["show", "--format=", "--patch", rev]
    diff = _run_git([*diff_args, "--"], cwd=cwd, timeout=120)
    if diff.returncode != 0:
        raise RuntimeError(_git_err(diff, diff_args[0]))
    if not diff.stdout.strip():
        return None
    pid = _run_git(["patch-id", "--stable"], cwd=cwd, input_text=diff.stdout, timeout=120)
    if pid.returncode != 0:
        raise RuntimeError(_git_err(pid, "patch-id"))
    tokens = pid.stdout.split()
    return tokens[0] if tokens else None


def _resolve_commit(cwd: Path, rev: str) -> str:
    proc = _run_git(["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"], cwd=cwd)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(f"git rev-parse: unknown commit {rev!r}")
    return proc.stdout.strip()


def verify_patch_continuity(
    repo_path: str | Path,
    verified_sha: str,
    head_sha: str,
    *,
    base_ref: Optional[str] = None,
    verified_base_ref: Optional[str] = None,
) -> dict[str, Any]:
    """Check whether `head_sha` carries the same patch as the verified commit (`git patch-id --stable`).

    A verdict stays valid across mechanical rebases when the stable patch-ids match.
    """
    try:
        cwd = Path(repo_path)
        verified_rev = _resolve_commit(cwd, _validate_git_ref(verified_sha, "verified_sha"))
        head_rev = _resolve_commit(cwd, _validate_git_ref(head_sha, "head_sha"))
        same_commit = verified_rev == head_rev
        verified_pid = compute_patch_id(cwd, verified_rev, base_ref=verified_base_ref or base_ref)
        head_pid = compute_patch_id(cwd, head_rev, base_ref=base_ref)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        return {
            "ok": False,
            "continuous": False,
            "verified_sha": verified_sha,
            "head_sha": head_sha,
            "error": str(exc),
        }
    if same_commit:
        continuous, reason = True, "same commit"
    elif verified_pid is None or head_pid is None:
        continuous, reason = False, "empty diff; patch-id unavailable"
    elif verified_pid == head_pid:
        continuous, reason = True, "patch-id match (mechanical rebase)"
    else:
        continuous, reason = False, "patch-id mismatch (content changed since verification)"
    return {
        "ok": True,
        "continuous": continuous,
        "same_commit": same_commit,
        "verified_sha": verified_rev,
        "head_sha": head_rev,
        "verified_patch_id": verified_pid,
        "head_patch_id": head_pid,
        "reason": reason,
    }


_WORKFLOW_DIR_B = b".github/workflows/"
_LOCKFILE_NAMES_B = frozenset(n.encode("ascii") for n in {
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lock", "bun.lockb",
    "poetry.lock", "pipfile.lock", "uv.lock", "pdm.lock", "cargo.lock", "gemfile.lock", "composer.lock",
    "go.sum", "flake.lock", "mix.lock", "pubspec.lock", "podfile.lock", "packages.lock.json", "gradle.lockfile",
})
_REBASE_REQUIRED_POLICIES = frozenset({"overlap_rebase_required", "workflow_rebase_required"})


def _is_lockfile(path: bytes) -> bool:
    return path.rsplit(b"/", 1)[-1].lower() in _LOCKFILE_NAMES_B


def assess_trunk_drift(
    repo_path: str | Path,
    head_ref: str = "HEAD",
    *,
    base_branch: Optional[str] = None,
    default_branch: str = "main",
    remote: str = "origin",
    fetch: bool = False,
) -> dict[str, Any]:
    """Compare files changed on `<remote>/<base>` vs. the PR since their merge-base.

    ``base_branch`` (a stacked PR's true ``baseRefName``) takes precedence over ``default_branch``.
    ``policy`` tiers the drift: ``clean`` (trunk not ahead), ``workflow_rebase_required`` (trunk touched
    ``.github/workflows/``), ``overlap_rebase_required`` (trunk and PR touched the same file),
    ``lockfile_only`` (trunk only touched lockfiles), ``disjoint`` (anything else), or ``unavailable`` on
    error; ``rebase_required`` is True only for the two ``*_rebase_required`` policies.
    """
    try:
        cwd = Path(repo_path)
        branch = _validate_default_branch(base_branch or default_branch)
        remote = _validate_remote(remote)
        head_ref = _validate_git_ref(head_ref, "head_ref")
        trunk_ref = f"{remote}/{branch}"
        if fetch:
            proc = _run_git(["fetch", remote, branch], cwd=cwd, timeout=120)
            if proc.returncode != 0:
                raise RuntimeError(_git_err(proc, "fetch"))
        proc = _run_git(["merge-base", trunk_ref, head_ref], cwd=cwd)
        if proc.returncode != 0 or not proc.stdout.strip():
            raise RuntimeError(_git_err(proc, "merge-base"))
        merge_base = proc.stdout.strip()
        proc = _run_git(["rev-list", "--count", f"{merge_base}..{trunk_ref}"], cwd=cwd)
        if proc.returncode != 0:
            raise RuntimeError(_git_err(proc, "rev-list"))
        ahead = int(proc.stdout.strip() or 0)

        def _names(rng: str) -> set[bytes]:
            # -z + bytes: Git paths are arbitrary bytes (not necessarily UTF-8); classify them raw so one
            # undecodable filename can never make the drift gate unavailable (fail open).
            out = run_git(
                ["--no-optional-locks", "diff", "--name-only", "-z", rng, "--"], cwd=cwd, timeout=60, text=False
            )
            if out.returncode != 0:
                detail = out.stderr.decode("utf-8", "replace").strip() or f"exit {out.returncode}"
                raise RuntimeError(f"git diff --name-only failed: {detail}")
            return {p for p in out.stdout.split(b"\0") if p}

        trunk_raw = _names(f"{merge_base}..{trunk_ref}")
        pr_raw = _names(f"{merge_base}..{head_ref}")
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        return {
            "ok": False, "has_drift": False, "has_overlap": False, "overlapping_files": [],
            "policy": "unavailable", "rebase_required": False, "workflow_files": [], "error": str(exc),
        }

    def _display(paths: Iterable[bytes]) -> list[str]:
        return sorted({p.decode("utf-8", "replace") for p in paths})

    overlap_raw = trunk_raw & pr_raw
    workflow_raw = {p for p in trunk_raw if p.startswith(_WORKFLOW_DIR_B)}
    overlapping = _display(overlap_raw)
    workflow_files = _display(workflow_raw)
    if ahead == 0:
        policy = "clean"
    elif workflow_raw:
        policy = "workflow_rebase_required"
    elif overlap_raw:
        policy = "overlap_rebase_required"
    elif trunk_raw and all(_is_lockfile(p) for p in trunk_raw):
        policy = "lockfile_only"
    else:
        policy = "disjoint"
    return {
        "ok": True,
        "trunk_ref": trunk_ref,
        "head_ref": head_ref,
        "merge_base": merge_base,
        "trunk_commits_ahead": ahead,
        "trunk_files": _display(trunk_raw),
        "pr_files": _display(pr_raw),
        "overlapping_files": overlapping,
        "has_drift": ahead > 0,
        "has_overlap": bool(overlapping),
        "policy": policy,
        "rebase_required": policy in _REBASE_REQUIRED_POLICIES,
        "workflow_files": workflow_files,
    }


def merge_pr(
    repo_spec: str,
    pr_number: int,
    *,
    expected_head_sha: Optional[str] = None,
    method: str = "squash",
    delete_branch: bool = True,
    tenant: Optional[str] = None,
    allow_patch_continuity: bool = False,
    allow_stacked_merge: bool = False,
    check_trunk_drift: bool = True,
    fetch: bool = False,
) -> dict[str, Any]:
    """Verify PR gates, merge via `gh pr merge`, then confirm the live PR state.

    Refuses a stacked PR (base is not the default branch) unless ``allow_stacked_merge=True``: merge
    stacks bottom-up (parent first, then restack). A live head that differs from ``expected_head_sha``
    is refused unless ``allow_patch_continuity=True`` and ``git patch-id --stable`` proves the live head
    carries the identical verified patch (the merge still pins the live head with
    ``--match-head-commit``). Rebase-required trunk drift (``check_trunk_drift``) blocks like any gate.

    Local sync targets the PR's true base branch (``baseRefName``), so a stacked PR fast-forwards
    its parent branch rather than the default branch. ``delete_branch=False`` keeps the head branch.

    Refuses on stale HEAD or any gate blocker. After `gh pr merge` exits 0 the PR is read
    back: only a confirmed ``MERGED`` state reports ``merged: True`` (and fast-forwards the
    registered local default branch when safe); queued/auto-merge PRs report
    ``queued: True``; a PR closed without merging is an error. A confirmed merge also
    captures a best-effort ``source="merge"`` metrics snapshot tagged with the repo and
    ``tenant`` (``metrics_snapshot_id``, ``None`` if the capture failed).
    """
    gate = inspect_pr_gate(
        repo_spec,
        pr_number,
        verified_head_sha=expected_head_sha or None,
        check_trunk_drift=check_trunk_drift,
        fetch=fetch,
    )
    if not gate.get("ok"):
        return {**gate, "merged": False, "queued": False}
    if gate.get("is_stacked") and not allow_stacked_merge:
        return {
            "ok": False,
            "merged": False,
            "queued": False,
            "error": (
                f"PR #{int(pr_number)} is stacked on {gate.get('base_branch')!r} "
                f"(default branch {gate.get('default_branch')!r}); merge the stack bottom-up and restack, "
                "or pass allow_stacked_merge=True to merge into the parent branch deliberately"
            ),
            "gate": gate,
        }
    continuity = gate.get("patch_continuity")
    if expected_head_sha and gate.get("head_sha") != expected_head_sha:
        if not (allow_patch_continuity and isinstance(continuity, dict) and continuity.get("continuous")):
            detail = ""
            if allow_patch_continuity and isinstance(continuity, dict):
                detail = f" (patch continuity not proven: {continuity.get('reason') or continuity.get('error')})"
            return {
                "ok": False,
                "merged": False,
                "queued": False,
                "error": (
                    f"HEAD mismatch: expected {expected_head_sha}, live PR is at {gate.get('head_sha')}{detail}"
                ),
                "gate": gate,
                "patch_continuity": continuity,
            }
    if not gate.get("ready_to_merge"):
        return {
            "ok": False,
            "merged": False,
            "queued": False,
            "error": "PR failed merge gate: " + "; ".join(gate.get("blockers") or []),
            "gate": gate,
        }

    flag = {"squash": "--squash", "merge": "--merge", "rebase": "--rebase"}.get(method, "--squash")
    args = ["pr", "merge", str(int(pr_number)), "--repo", str(gate["repo"]), flag]
    if delete_branch:
        args.append("--delete-branch")
    if gate.get("head_sha"):
        args.extend(["--match-head-commit", str(gate["head_sha"])])

    proc = _run_gh(args, timeout=60)
    if proc.returncode != 0:
        return {
            "ok": False,
            "merged": False,
            "queued": False,
            "error": (proc.stderr or proc.stdout or f"gh merge exit {proc.returncode}").strip(),
            "gate": gate,
        }

    base_result = {
        "repo": gate["repo"],
        "pr_number": gate["pr_number"],
        "url": gate["url"],
        "head_sha": gate["head_sha"],
        "method": method,
        "base_branch": gate.get("base_branch"),
        "is_stacked": bool(gate.get("is_stacked")),
        "delete_branch": bool(delete_branch),
    }
    if expected_head_sha and gate.get("head_sha") != expected_head_sha:
        base_result["expected_head_sha"] = expected_head_sha
        base_result["patch_continuity"] = continuity
    readback = _run_gh([
        "pr", "view", str(int(pr_number)), "--repo", str(gate["repo"]), "--json", "state,mergedAt,baseRefName",
    ])
    live: Any = None
    if readback.returncode == 0:
        try:
            live = json.loads(readback.stdout or "")
        except ValueError:
            live = None
    if not isinstance(live, dict):
        detail = (readback.stderr or readback.stdout or "").strip() if readback.returncode != 0 else "invalid JSON"
        return {
            "ok": False,
            "merged": False,
            "queued": False,
            "error": f"gh pr merge exited 0 but live PR state readback failed: {detail}",
            "gate": gate,
        }

    live_state = str(live.get("state") or "").upper()
    if live_state == "MERGED" and live.get("mergedAt"):
        repo_entry = get_repo(repo_spec) or get_repo(str(gate["repo"]))
        branch = (
            live.get("baseRefName")
            or gate.get("base_branch")
            or (repo_entry or {}).get("default_branch")
            or "main"
        )
        local_path = Path(str(repo_entry["path"])) if repo_entry and repo_entry.get("path") else None
        if local_path is not None and (local_path / ".git").exists():
            local_synced, local_sync_note = _sync_local_default_branch(local_path, str(branch))
        else:
            local_synced, local_sync_note = False, "skipped: local checkout not registered or missing"
        snapshot = capture_metrics_snapshot(
            source="merge",
            repo_slug=str((repo_entry or {}).get("slug") or gate["repo"]),
            tenant=tenant,
            extra={"pr_number": gate["pr_number"], "head_sha": gate["head_sha"], "url": gate["url"]},
        )
        return {
            "ok": True,
            "merged": True,
            "queued": False,
            **base_result,
            "base_branch": str(branch),
            "state": live.get("state"),
            "merged_at": live.get("mergedAt"),
            "local_synced": local_synced,
            "local_sync_note": local_sync_note,
            "metrics_snapshot_id": (snapshot or {}).get("id"),
        }
    if live_state == "CLOSED":
        return {
            "ok": False,
            "merged": False,
            "queued": False,
            "error": "PR is CLOSED without merging after gh pr merge",
            "gate": gate,
        }
    return {
        "ok": True,
        "merged": False,
        "queued": True,
        **base_result,
        "state": live.get("state"),
        "local_synced": False,
        "local_sync_note": "skipped: PR queued or not yet merged",
    }


# ---------------------------------------------------------------------------
# Explicit, fail-closed publish of a verified local branch (publish_policy: ask)
# ---------------------------------------------------------------------------

_PR_URL_RE = re.compile(r"https://\S+/pull/(\d+)\b")
_PUBLISH_EVIDENCE_KEYS = (
    "repo", "branch", "base_branch", "head_sha", "expected_head_sha", "pr_number", "url", "draft",
)


_validate_full_sha = validate_full_sha


def _echo(value: Any) -> Optional[str]:
    """Bounded echo of caller input into results/trail evidence before it is validated."""
    # Lone surrogates are replaced and NULs dropped so a refusal can still be written to trail.db.
    if not isinstance(value, str):
        return None
    return value[:200].encode("utf-8", "replace").decode("utf-8").replace("\x00", "")


def _trail_safe(value: Any) -> str:
    """Untruncated UTF-8-safe, NUL-free form of a trail context field; non-strings become ``""``."""
    if not isinstance(value, str):
        return ""
    return value.encode("utf-8", "replace").decode("utf-8").replace("\x00", "")


_validate_branch_field = validate_branch_field
_require_clean_str = require_clean_str  # patch seam: publish_pr resolves this name at call time


def _validate_git_ref_name(cwd: Path, name: str, field: str) -> None:
    """Refuse anything git would not accept as a plain branch name (via the ``_run_git`` seam)."""
    validate_git_ref_name(cwd, name, field, run_git=_run_git)


def _record_publish(result: dict[str, Any], *, outcome: str, reason: str, repo_slug: str,
                    tenant: Optional[str], card_id: Optional[str], actor: Optional[str]) -> None:
    from .trail import record_transition

    evidence: dict[str, Any] = {k: result.get(k) for k in _PUBLISH_EVIDENCE_KEYS}
    # The `gate` artifact keeps every row (refusals included) auditable as a high-stakes decision.
    evidence["gate"] = {
        "check": reason,
        "pushed": bool(result.get("pushed")),
        "created": bool(result.get("created")),
        "published": bool(result.get("published")),
    }
    if outcome == "published":
        why = f"Published PR #{result.get('pr_number')} at verified HEAD {result.get('head_sha')}"
    else:
        why = str(result.get("error") or f"publish {outcome}")
    # Sanitized independently of preflight so the row lands even when these fields were the bad input.
    record_transition(
        "publish_pr",
        tenant=_trail_safe(tenant),
        repo_slug=repo_slug,
        phase="publish",
        card_id=_trail_safe(card_id),
        actor=_trail_safe(actor).strip() or "orchestrator",
        why=why[:500],
        evidence=evidence,
        result=outcome,
    )


def publish_pr(
    repo_spec: str,
    branch: str,
    *,
    expected_head_sha: str,
    title: str,
    body: Optional[str] = None,
    body_file: Optional[str] = None,
    base_branch: Optional[str] = None,
    draft: bool = False,
    remote: str = "origin",
    tenant: Optional[str] = None,
    card_id: Optional[str] = None,
    actor: Optional[str] = "orchestrator",
) -> dict[str, Any]:
    """Push a verified local branch by exact SHA and open its GitHub PR (explicit publish step).

    Every precondition is checked before any remote call: valid inputs, a registered repo with a
    local checkout, ``branch`` distinct from the target base, and ``refs/heads/<branch>`` resolving
    to exactly ``expected_head_sha``. Any failure returns ``ok: False`` with ``refused: True`` and
    nothing pushed. The push sends ``<expected_head_sha>:refs/heads/<branch>`` (never the moving
    branch name), the body travels via a temporary ``--body-file`` that is always removed, and
    success is reported only after ``gh pr view`` confirms the live PR head and base. Each call
    appends a ``publish_pr`` row (``phase="publish"``) to ``trail.db``.
    """
    result: dict[str, Any] = {
        "ok": False,
        "published": False,
        "pushed": False,
        "created": False,
        "refused": False,
        "repo": None,
        "pr_number": None,
        "url": None,
        "head_sha": None,
        "expected_head_sha": _echo(expected_head_sha),
        "branch": _echo(branch),
        "base_branch": _echo(base_branch),
        "draft": draft if isinstance(draft, bool) else None,
        "state": None,
    }
    # trail.record_transition maps a registered spec to its slug and keeps unknown specs verbatim.
    repo_slug = (_echo(repo_spec.strip()) or "") if isinstance(repo_spec, str) else ""

    def finish(outcome: str, reason: str, error: Optional[str] = None) -> dict[str, Any]:
        if error:
            result["error"] = error
        result["refused"] = outcome == "refused"
        _record_publish(result, outcome=outcome, reason=reason, repo_slug=repo_slug,
                        tenant=tenant, card_id=card_id, actor=actor)
        return result

    try:
        for field, value, optional in (
            ("repo", repo_spec, False), ("branch", branch, False), ("expected_head_sha", expected_head_sha, False),
            ("title", title, False), ("body", body, True), ("body_file", body_file, True),
            ("base_branch", base_branch, True), ("remote", remote, False),
            ("tenant", tenant, True), ("card_id", card_id, True),
        ):
            _require_clean_str(value, field, optional=optional)
        _require_clean_str(actor, "actor", optional=True, non_empty=True)
        spec = repo_spec.strip()
        if not spec:
            raise ValueError("repo must be a registered slug or 'owner/repo'")
        if not isinstance(draft, bool):
            raise ValueError(f"draft must be a boolean; got {type(draft).__name__}")
        clean_branch = _validate_branch_field(branch, "branch")
        result["branch"] = clean_branch
        clean_remote = _validate_remote(remote)
        sha = _validate_full_sha(expected_head_sha)
        result["expected_head_sha"] = sha
        clean_title = single_line(title) if isinstance(title, str) else ""
        if not clean_title:
            raise ValueError("title must be a non-empty single line")
        if body is not None and body_file is not None:
            raise ValueError("pass either body or body_file, not both")
        clean_base = _validate_branch_field(base_branch, "base_branch") if base_branch is not None else None
    except ValueError as exc:
        return finish("refused", "invalid_input", str(exc))

    if body_file is not None:
        if not (isinstance(body_file, str) and body_file.strip()):
            return finish("refused", "invalid_input", f"body_file must be a non-empty path; got {_echo(body_file)!r}")
        try:
            body_text = Path(body_file).expanduser().read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            return finish("refused", "body_file_unreadable", f"cannot read body_file {body_file[:200]!r}: {exc}")
    else:
        body_text = body or ""
    try:
        _require_clean_str(body_text, "body_file content" if body_file is not None else "body")
    except ValueError as exc:
        return finish("refused", "invalid_input", str(exc))

    entry, cwd = _registered_checkout(spec)
    if entry is None:
        try:
            result["repo"], repo_slug = normalize_repo(spec)
        except ValueError:
            pass
        return finish("refused", "unregistered_repo", f"Repository {spec!r} is not registered with Factonio")
    repo_slug = str(entry.get("slug") or "")
    full_repo = str(entry.get("repo") or "")
    if not OWNER_REPO_RE.match(full_repo):
        return finish("refused", "unregistered_repo", f"Registered entry for {spec!r} has no valid owner/repo")
    result["repo"] = full_repo
    if cwd is None:
        return finish("refused", "missing_checkout", f"Local checkout for {full_repo} is not registered or missing")
    try:
        target_base = clean_base or _validate_default_branch(str(entry.get("default_branch") or "main"))
    except ValueError as exc:
        return finish("refused", "invalid_input", str(exc))
    result["base_branch"] = target_base
    try:
        _require_clean_str(target_base, "base_branch")
        _validate_git_ref_name(cwd, clean_branch, "branch")
        _validate_git_ref_name(cwd, target_base, "base_branch")
    except ValueError as exc:
        return finish("refused", "invalid_input", str(exc))
    except (OSError, subprocess.SubprocessError) as exc:
        return finish("refused", "git_unavailable", f"cannot validate branch names: {exc}")
    if clean_branch == target_base:
        return finish(
            "refused", "branch_is_base",
            f"Refusing to publish {clean_branch!r}: it is the target base branch; publish a feature branch",
        )

    try:
        local_head = _verify_commit(cwd, f"refs/heads/{clean_branch}")
    except (OSError, subprocess.SubprocessError) as exc:
        return finish("refused", "git_unavailable", f"cannot inspect local branch: {exc}")
    if not local_head:
        return finish("refused", "missing_branch", f"Local branch refs/heads/{clean_branch} does not exist in {cwd}")
    result["head_sha"] = local_head.lower()
    if local_head.lower() != sha:
        return finish(
            "refused", "head_drift",
            f"HEAD drift: refs/heads/{clean_branch} is at {local_head}, expected verified {sha}",
        )

    try:
        configured = _is_configured_remote(cwd, clean_remote)
    except (OSError, subprocess.SubprocessError) as exc:
        return finish("refused", "git_unavailable", f"cannot list git remotes: {exc}")
    if not configured:
        return finish(
            "refused", "unknown_remote",
            f"remote {clean_remote!r} is not a configured git remote in {cwd}; refusing to push",
        )

    try:
        push = _run_git(["push", clean_remote, f"{sha}:refs/heads/{clean_branch}"], cwd=cwd, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return finish("error", "push_failed", f"git push failed: {exc}")
    if push.returncode != 0:
        return finish("error", "push_failed", _git_err(push, "push"))
    result["pushed"] = True

    tmp_path: Optional[str] = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix="factonio-pr-body-", suffix=".md")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body_text)
        args = [
            "pr", "create", "--repo", full_repo, "--base", target_base, "--head", clean_branch,
            "--title", clean_title, "--body-file", tmp_path,
        ]
        if draft:
            args.append("--draft")
        created = _run_gh(args, timeout=60)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        # ValueError covers UnicodeError on the body write; preflight should already have refused it.
        return finish("error", "create_failed", f"gh pr create failed: {exc}")
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass
    if created.returncode != 0:
        detail = (created.stderr or created.stdout or f"exit {created.returncode}").strip()
        return finish("error", "create_failed", f"gh pr create failed: {detail}")
    result["created"] = True

    urls = _PR_URL_RE.findall(created.stdout or "")
    pr_ref = urls[-1] if urls else clean_branch
    live: Any = None
    detail = "invalid JSON"
    try:
        view = _run_gh(
            ["pr", "view", pr_ref, "--repo", full_repo, "--json", "number,url,headRefOid,baseRefName,state,isDraft"],
            timeout=30,
        )
        if view.returncode == 0:
            live = json.loads(view.stdout or "")
        else:
            detail = (view.stderr or view.stdout or f"exit {view.returncode}").strip()
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        detail = str(exc)
    if not isinstance(live, dict):
        return finish("error", "readback_failed", f"gh pr create succeeded but live PR readback failed: {detail}")

    number = _comment_id(live.get("number"))
    result["pr_number"] = number if number and number > 0 else None
    result["url"] = str(live.get("url") or "") or None
    result["state"] = live.get("state")
    result["draft"] = bool(live.get("isDraft"))
    live_head = str(live.get("headRefOid") or "").lower()
    live_base = str(live.get("baseRefName") or "")
    if result["pr_number"] is None or live_head != sha or live_base != target_base:
        return finish(
            "error", "readback_mismatch",
            f"live PR does not match the verified publish (head {live_head or '?'} vs {sha}, "
            f"base {live_base or '?'} vs {target_base})",
        )
    result["head_sha"] = live_head
    result["ok"] = True
    result["published"] = True
    return finish("published", "published")


# ---------------------------------------------------------------------------
# Stacked PRs: branch seeding, true-base patch-ids, and clean-worktree restacks
# ---------------------------------------------------------------------------


def _verify_commit(cwd: Path, rev: str) -> Optional[str]:
    proc = _run_git(["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"], cwd=cwd)
    sha = proc.stdout.strip() if proc.returncode == 0 else ""
    return sha or None


def _fetch_refspec(cwd: Path, remote: str, refspec: str) -> bool:
    return _run_git(["fetch", "--no-tags", remote, refspec], cwd=cwd, timeout=120).returncode == 0


def _fetch_branch(cwd: Path, remote: str, branch: str) -> bool:
    return _fetch_refspec(cwd, remote, f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}")


def _fetch_pull_head(cwd: Path, remote: str, pr_number: int) -> bool:
    n = int(pr_number)
    return _fetch_refspec(cwd, remote, f"+refs/pull/{n}/head:refs/factonio/pull/{n}/head")


def _registered_checkout(repo_spec: str) -> tuple[Optional[dict[str, Any]], Optional[Path]]:
    entry = get_repo(repo_spec)
    if entry and entry.get("path"):
        path = Path(str(entry["path"])).expanduser()
        if (path / ".git").exists():
            return entry, path
    return entry, None


_validate_pr_number = validate_pr_number


def seed_stacked_branch(
    repo_path: str | Path,
    base_branch: str,
    branch_name: str,
    *,
    remote: str = "origin",
) -> dict[str, Any]:
    """Create local ``branch_name`` at the tip of ``base_branch`` without touching any checkout.

    Resolves ``refs/heads/<base_branch>`` then ``refs/remotes/<remote>/<base_branch>``. Hermes's
    worktree dispatcher checks out an existing branch as-is, so a seeded branch anchors a stacked
    card's worktree on its parent. Raises ``ValueError`` only for unsafe names; every git problem
    (not a repo, base ref missing) returns ``{"ok": False, "seeded": False, "reason": ...}``.
    """
    base = _validate_default_branch(base_branch)
    branch = _validate_default_branch(branch_name)
    remote = _validate_remote(remote)
    out: dict[str, Any] = {
        "ok": False, "seeded": False, "branch_name": branch, "base_branch": base,
        "base_commit": None, "base_ref": None,
    }
    cwd = Path(repo_path).expanduser()
    try:
        if not (cwd / ".git").exists():
            return {**out, "reason": f"not a git repository: {cwd}"}
        probe = _run_git(["rev-parse", "--is-inside-work-tree"], cwd=cwd)
        if probe.returncode != 0:
            return {**out, "reason": _git_err(probe, "rev-parse")}
        fmt = _run_git(["check-ref-format", "--branch", branch], cwd=cwd)
        if fmt.returncode != 0:
            return {**out, "reason": f"invalid branch name {branch!r}"}
        existing = _verify_commit(cwd, f"refs/heads/{branch}")
        if existing:
            return {**out, "ok": True, "already_exists": True, "base_commit": existing,
                    "base_ref": f"refs/heads/{branch}", "reason": "branch already exists"}
        for ref in (f"refs/heads/{base}", f"refs/remotes/{remote}/{base}"):
            sha = _verify_commit(cwd, ref)
            if sha:
                made = _run_git(["branch", "--no-track", branch, sha], cwd=cwd)
                if made.returncode != 0:
                    return {**out, "reason": _git_err(made, "branch")}
                return {**out, "ok": True, "seeded": True, "base_commit": sha, "base_ref": ref}
        return {**out, "reason": f"base branch {base!r} not found locally (refs/heads or {remote}/)"}
    except (OSError, subprocess.SubprocessError) as exc:
        return {**out, "reason": str(exc)}


def inspect_pr_patch_id(
    repo_spec: str,
    pr_number: int,
    *,
    base_ref: Optional[str] = None,
    remote: str = "origin",
    fetch: bool = False,
) -> dict[str, Any]:
    """Stable patch-id of a live PR measured against its true base (``<remote>/<baseRefName>``)."""
    gate = inspect_pr_gate(repo_spec, pr_number, check_trunk_drift=False)
    if not gate.get("ok"):
        return {**gate, "patch_id": None}
    out: dict[str, Any] = {
        "ok": False,
        "repo": gate["repo"],
        "pr_number": gate["pr_number"],
        "base_branch": gate.get("base_branch"),
        "default_branch": gate.get("default_branch"),
        "is_stacked": gate.get("is_stacked"),
        "head_sha": gate.get("head_sha"),
        "base_ref": None,
        "merge_base": None,
        "patch_id": None,
    }
    try:
        remote = _validate_remote(remote)
        _, cwd = _registered_checkout(repo_spec)
        if cwd is None:
            return {**out, "error": "local checkout not registered or missing"}
        base_branch = _validate_default_branch(gate.get("base_branch") or gate.get("default_branch") or "main")
        head = _validate_git_ref(str(gate.get("head_sha") or ""), "head_sha")
        if fetch:
            _fetch_branch(cwd, remote, base_branch)
            if not _verify_commit(cwd, head):
                if not (gate.get("head_branch") and _fetch_branch(cwd, remote, str(gate["head_branch"]))):
                    _fetch_pull_head(cwd, remote, int(pr_number))
        resolved_base = _validate_git_ref(base_ref, "base_ref") if base_ref else f"{remote}/{base_branch}"
        if not _verify_commit(cwd, head):
            return {**out, "base_ref": resolved_base, "error": f"PR head {head} is not available locally (try fetch=True)"}
        mb = _run_git(["merge-base", resolved_base, head], cwd=cwd)
        if mb.returncode != 0 or not mb.stdout.strip():
            return {**out, "base_ref": resolved_base, "error": _git_err(mb, "merge-base")}
        patch_id = compute_patch_id(cwd, head, base_ref=resolved_base)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        return {**out, "error": str(exc)}
    return {**out, "ok": True, "base_ref": resolved_base, "merge_base": mb.stdout.strip(), "patch_id": patch_id}


def _gh_pr_view(full_repo: str, pr_number: int, fields: str) -> Optional[dict[str, Any]]:
    try:
        proc = _run_gh(["pr", "view", str(int(pr_number)), "--repo", full_repo, "--json", fields])
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout or "")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _resolve_old_base(
    cwd: Path,
    full_repo: str,
    *,
    remote: str,
    old_base_sha: Optional[str],
    parent_pr: Optional[int],
    old_base_branch: Optional[str],
    target_base: str,
) -> tuple[Optional[str], str]:
    """Commit the PR's own commits were stacked on: explicit sha -> parent PR head -> old base branch."""
    if old_base_sha:
        ref = _validate_git_ref(old_base_sha, "old_base_sha")
        sha = _verify_commit(cwd, ref)
        if not sha and _fetch_refspec(cwd, remote, ref):
            sha = _verify_commit(cwd, ref)
        if not sha:
            raise RuntimeError(f"old_base_sha {ref!r} is not available locally")
        return sha, "old_base_sha"
    if parent_pr is not None:
        parent = _gh_pr_view(full_repo, parent_pr, "headRefOid,headRefName,baseRefName,state") or {}
        oid = str(parent.get("headRefOid") or "")
        if oid:
            sha = _verify_commit(cwd, oid)
            if not sha and _fetch_pull_head(cwd, remote, parent_pr):
                sha = _verify_commit(cwd, oid)
            if sha:
                return sha, f"parent_pr #{int(parent_pr)}"
    if old_base_branch and old_base_branch != target_base:
        _fetch_branch(cwd, remote, old_base_branch)
        sha = _verify_commit(cwd, f"refs/remotes/{remote}/{old_base_branch}")
        if sha:
            return sha, f"{remote}/{old_base_branch}"
    return None, "none"


def _record_restack(result: dict[str, Any], *, repo_slug: str, tenant: Optional[str],
                    card_id: Optional[str], actor: Optional[str]) -> None:
    from .trail import record_transition

    if result.get("conflict"):
        outcome = "conflict"
    elif not result.get("ok"):
        outcome = "error"
    else:
        outcome = "continuous" if result.get("verdict_continuous") else "reverify_required"
    why = result.get("error") or (
        f"PR #{result.get('pr_number')} restacked onto {result.get('target_base')}: "
        + ("patch-id unchanged" if result.get("verdict_continuous") else "patch-id changed; re-verify")
    )
    keys = (
        "pr_number", "head_branch", "old_head_sha", "new_head_sha", "old_base_branch", "target_base",
        "old_base_commit", "old_base_source", "rebase_mode", "before_patch_id", "after_patch_id",
        "verdict_continuous", "reverify_required", "conflict", "conflict_files", "pushed", "retargeted", "dry_run",
    )
    record_transition(
        "restack_pr",
        tenant=tenant or "",
        repo_slug=repo_slug,
        phase="gate",
        card_id=card_id or "",
        actor=actor,
        why=str(why)[:500],
        evidence={k: result.get(k) for k in keys},
        result=outcome,
    )


def restack_pr(
    repo_spec: str,
    pr_number: int,
    *,
    new_base: Optional[str] = None,
    old_base_sha: Optional[str] = None,
    parent_pr: Optional[int] = None,
    verified_head_sha: Optional[str] = None,
    remote: str = "origin",
    push: bool = True,
    retarget: bool = True,
    dry_run: bool = False,
    tenant: Optional[str] = None,
    card_id: Optional[str] = None,
    actor: Optional[str] = None,
) -> dict[str, Any]:
    """Rebase an open PR onto ``new_base`` in a throwaway linked worktree and report verdict continuity.

    The registered checkout is never checked out or modified: the rebase runs in a temporary
    ``git worktree add --detach`` that is always removed. With a known old base commit
    (``old_base_sha`` -> ``parent_pr`` head -> ``<remote>/<old base>``) the rebase is
    ``--onto <remote>/<new_base> <old_base>``, which replays only the PR's own commits even when the
    parent was squash-merged and its branch deleted. ``verdict_continuous`` is true when the stable
    patch-id before (vs. the old base) equals the patch-id after (vs. the new base).
    """
    pr_number = _validate_pr_number(pr_number)
    if parent_pr is not None:
        parent_pr = _validate_pr_number(parent_pr, "parent_pr")
    remote = _validate_remote(remote)
    if new_base is not None:
        new_base = _validate_default_branch(new_base)
    if verified_head_sha is not None:
        verified_head_sha = _validate_git_ref(verified_head_sha, "verified_head_sha")
    if old_base_sha is not None:
        old_base_sha = _validate_git_ref(old_base_sha, "old_base_sha")

    gate = inspect_pr_gate(repo_spec, pr_number, check_trunk_drift=False)
    entry, cwd = _registered_checkout(repo_spec)
    full_repo = str(gate.get("repo") or (entry or {}).get("repo") or normalize_repo(repo_spec)[0])
    repo_slug = str((entry or {}).get("slug") or normalize_repo(full_repo)[1])
    result: dict[str, Any] = {
        "ok": False,
        "repo": full_repo,
        "pr_number": pr_number,
        "head_branch": gate.get("head_branch"),
        "old_head_sha": gate.get("head_sha"),
        "new_head_sha": None,
        "old_base_branch": gate.get("base_branch"),
        "target_base": None,
        "old_base_commit": None,
        "old_base_source": None,
        "rebase_mode": None,
        "before_patch_id": None,
        "after_patch_id": None,
        "verdict_continuous": False,
        "reverify_required": True,
        "conflict": False,
        "conflict_files": [],
        "pushed": False,
        "retargeted": False,
        "dry_run": bool(dry_run),
        "noop": False,
    }

    def finish(**updates: Any) -> dict[str, Any]:
        result.update(updates)
        _record_restack(result, repo_slug=repo_slug, tenant=tenant, card_id=card_id, actor=actor)
        return result

    if not gate.get("ok"):
        return finish(error=str(gate.get("error") or "PR gate inspection failed"))
    if str(gate.get("state") or "").upper() != "OPEN":
        return finish(error=f"PR #{pr_number} is {gate.get('state')}; only OPEN PRs can be restacked")
    if cwd is None:
        return finish(error="local checkout not registered or missing")
    try:
        head_branch = _validate_default_branch(str(gate.get("head_branch") or ""))
        old_head = _validate_git_ref(str(gate.get("head_sha") or ""), "head_sha")
        target_base = _validate_default_branch(new_base or gate.get("default_branch") or "main")
    except ValueError as exc:
        return finish(error=str(exc))
    old_base_branch = gate.get("base_branch")
    result["target_base"] = target_base
    target_ref = f"{remote}/{target_base}"

    if not _fetch_branch(cwd, remote, target_base) and not _verify_commit(cwd, f"refs/remotes/{target_ref}"):
        return finish(error=f"cannot fetch {remote} {target_base}")
    if not _verify_commit(cwd, old_head):
        if not _fetch_branch(cwd, remote, head_branch) or not _verify_commit(cwd, old_head):
            _fetch_pull_head(cwd, remote, pr_number)
    if not _verify_commit(cwd, old_head):
        return finish(error=f"PR head {old_head} is not available locally after fetch")

    try:
        old_base_commit, source = _resolve_old_base(
            cwd, full_repo, remote=remote, old_base_sha=old_base_sha, parent_pr=parent_pr,
            old_base_branch=str(old_base_branch) if old_base_branch else None, target_base=target_base,
        )
    except (ValueError, RuntimeError) as exc:
        return finish(error=str(exc))
    result.update(old_base_commit=old_base_commit, old_base_source=source)

    before_ref = old_base_commit or (f"{remote}/{old_base_branch}" if old_base_branch else target_ref)
    try:
        result["before_patch_id"] = compute_patch_id(cwd, verified_head_sha or old_head, base_ref=before_ref)
    except (ValueError, RuntimeError) as exc:
        result["before_patch_note"] = str(exc)

    tmp_root = Path(tempfile.mkdtemp(prefix="factonio-restack-"))
    wt = tmp_root / "wt"
    result["temp_worktree"] = str(wt)
    try:
        added = _run_git(["worktree", "add", "--detach", str(wt), old_head], cwd=cwd, timeout=120)
        if added.returncode != 0:
            return finish(error=_git_err(added, "worktree add"))
        rebase_args = ["-c", "rebase.updateRefs=false", "-c", "rebase.autoStash=false", "rebase", "--no-autosquash"]
        if old_base_commit:
            result["rebase_mode"] = "onto"
            rebase_args += ["--onto", target_ref, old_base_commit]
        else:
            result["rebase_mode"] = "plain"
            rebase_args.append(target_ref)
        rebased = _run_git(rebase_args, cwd=wt, timeout=300)
        if rebased.returncode != 0:
            unmerged = _run_git(["diff", "--name-only", "--diff-filter=U"], cwd=wt)
            files = [line for line in unmerged.stdout.splitlines() if line.strip()]
            _run_git(["rebase", "--abort"], cwd=wt, timeout=60)
            output = (rebased.stderr or rebased.stdout or "").strip()
            conflict = bool(files) or "conflict" in output.lower()
            last_line = (output.splitlines() or ["unknown"])[-1]
            return finish(
                conflict=conflict,
                conflict_files=files,
                error=("rebase conflict: " if conflict else "rebase failed: ") + last_line,
            )
        new_head = _verify_commit(wt, "HEAD")
        if not new_head:
            return finish(error="cannot resolve rebased HEAD")
        result["new_head_sha"] = new_head
        result["noop"] = new_head == old_head
        try:
            result["after_patch_id"] = compute_patch_id(wt, new_head, base_ref=target_ref)
        except (ValueError, RuntimeError) as exc:
            result["after_patch_note"] = str(exc)
        continuous = bool(
            result["before_patch_id"] and result["after_patch_id"]
            and result["before_patch_id"] == result["after_patch_id"]
        )
        result.update(verdict_continuous=continuous, reverify_required=not continuous)

        if not dry_run and push and not result["noop"]:
            pushed = _run_git(
                ["push", f"--force-with-lease=refs/heads/{head_branch}:{old_head}", remote,
                 f"{new_head}:refs/heads/{head_branch}"],
                cwd=wt, timeout=180,
            )
            if pushed.returncode != 0:
                return finish(error=_git_err(pushed, "push --force-with-lease"))
            result["pushed"] = True
        if not dry_run and retarget and old_base_branch != target_base:
            edit = _run_gh(["pr", "edit", str(pr_number), "--repo", full_repo, "--base", target_base], timeout=60)
            if edit.returncode != 0:
                detail = (edit.stderr or edit.stdout or f"gh exit {edit.returncode}").strip()
                return finish(error=f"gh pr edit --base {target_base} failed: {detail}")
            result["retargeted"] = True
        return finish(ok=True)
    except (OSError, subprocess.SubprocessError) as exc:
        return finish(error=str(exc))
    finally:
        if wt.exists():
            _run_git(["worktree", "remove", "--force", str(wt)], cwd=cwd, timeout=120)
        shutil.rmtree(tmp_root, ignore_errors=True)
        _run_git(["worktree", "prune"], cwd=cwd)


def restack_stack(
    repo_spec: str,
    *,
    parent_pr: Optional[int] = None,
    from_branch: Optional[str] = None,
    new_base: Optional[str] = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Restack every open PR targeting ``from_branch`` (default: the parent PR's head branch).

    With ``parent_pr``, the parent's head commit becomes each child's ``old_base_sha`` and the
    parent's base branch is the default ``new_base`` (where the parent landed).
    """
    if parent_pr is not None:
        parent_pr = _validate_pr_number(parent_pr, "parent_pr")
    if from_branch is not None:
        from_branch = _validate_default_branch(from_branch)
    if new_base is not None:
        new_base = _validate_default_branch(new_base)
    entry = get_repo(repo_spec)
    full_repo = str(entry["repo"]) if entry else normalize_repo(repo_spec)[0]
    out: dict[str, Any] = {
        "ok": False, "repo": full_repo, "parent_pr": parent_pr, "from_branch": from_branch,
        "new_base": new_base, "results": [],
    }
    if parent_pr is not None:
        parent = _gh_pr_view(full_repo, parent_pr, "headRefOid,headRefName,baseRefName,state")
        if parent is None:
            return {**out, "error": f"cannot read parent PR #{parent_pr}"}
        from_branch = from_branch or (str(parent.get("headRefName") or "") or None)
        new_base = new_base or (str(parent.get("baseRefName") or "") or None)
        if parent.get("headRefOid") and not kwargs.get("old_base_sha"):
            kwargs["old_base_sha"] = str(parent["headRefOid"])
        out.update(from_branch=from_branch, new_base=new_base, parent_head_sha=parent.get("headRefOid"))
    if not from_branch:
        raise ValueError("restack_stack needs parent_pr or from_branch")
    proc = _run_gh([
        "pr", "list", "--repo", full_repo, "--base", from_branch, "--state", "open",
        "--json", "number,headRefName,headRefOid,baseRefName", "--limit", "100",
    ])
    if proc.returncode != 0:
        return {**out, "error": f"gh pr list failed: {(proc.stderr or proc.stdout or '').strip()[:200]}"}
    try:
        children = json.loads(proc.stdout or "[]")
    except ValueError:
        return {**out, "error": "gh pr list returned invalid JSON"}
    numbers = sorted(int(c["number"]) for c in children if isinstance(c, dict) and c.get("number"))
    results = [
        restack_pr(repo_spec, n, new_base=new_base, parent_pr=parent_pr, **kwargs) for n in numbers
    ]
    return {**out, "ok": all(r.get("ok") for r in results), "results": results}


# ---------------------------------------------------------------------------
# Worktree audit & safety-gated cleanup
# ---------------------------------------------------------------------------

_INACTIVE_CARD_STATUSES = INACTIVE_CARD_STATUSES
# Ignored paths that tooling regenerates on demand. Any other ignored file (``.env``, local
# databases, data dumps) is unique local data and holds the worktree like a tracked edit.
_REGENERABLE_IGNORED_DIRS: tuple[str, ...] = (
    "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", "node_modules", ".venv", "venv",
    "build", "dist", ".tox", ".eggs", "*.egg-info",
)
_REGENERABLE_IGNORED_FILES: tuple[str, ...] = ("*.pyc", ".coverage", ".DS_Store")
_MAX_LISTED_PATHS = 50


def _human_size(num_bytes: int) -> str:
    size = float(max(0, int(num_bytes)))
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def _dir_size_bytes(path: Path) -> int:
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    else:
                        total += st.st_size
        except OSError:
            continue
    return total


def _parse_worktree_porcelain(text: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in text.splitlines():
        if not line.strip():
            if current:
                entries.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            if current:
                entries.append(current)
            current = {"path": value}
        elif key == "HEAD":
            current["head_sha"] = value
        elif key == "branch":
            current["branch"] = value[len("refs/heads/"):] if value.startswith("refs/heads/") else value
        elif key in ("detached", "bare"):
            current[key] = True
        elif key in ("locked", "prunable"):
            current[key] = value or True
    if current:
        entries.append(current)
    return entries


def _is_regenerable_ignored(rel: str) -> bool:
    parts = [p for p in rel.strip("/").split("/") if p]
    if not parts:
        return False
    if any(fnmatch.fnmatchcase(part, pat) for part in parts for pat in _REGENERABLE_IGNORED_DIRS):
        return True
    return any(fnmatch.fnmatchcase(parts[-1], pat) for pat in _REGENERABLE_IGNORED_FILES)


def _dirty_inventory(wt_path: Path) -> dict[str, Any]:
    """Classify a worktree's local state, including ignored files.

    ``wip:N`` counts tracked edits plus ignored files outside the regenerable allowlist;
    ``scratch:N`` counts untracked (non-ignored) files when nothing protected exists.
    """
    proc = _run_git_ro(["status", "--porcelain=v1", "-z", "--ignored=matching", "--untracked-files=all"], cwd=wt_path)
    if proc.returncode != 0:
        # Unknown state must never look prunable.
        return {"dirty": "wip:?", "tracked": [], "ignored_data": [], "scratch": [], "hold_reason": "git status failed"}
    tracked: list[str] = []
    ignored_data: list[str] = []
    scratch: list[str] = []
    nested_repos: list[str] = []
    for code, rel in parse_status_z(proc.stdout):
        if code in ("??", "!!") and rel.endswith("/") and (wt_path / rel / ".git").exists():
            # Nested repository/worktree: git lists it as one opaque entry, but it holds its own history.
            nested_repos.append(rel.rstrip("/"))
        elif code == "??":
            scratch.append(rel)
        elif code == "!!":
            if not _is_regenerable_ignored(rel):
                ignored_data.append(rel)
        else:
            tracked.append(rel)
    wip = len(tracked) + len(ignored_data)
    if wip:
        dirty = f"wip:{wip}"
        parts = []
        if tracked:
            parts.append(f"{len(tracked)} tracked edit(s)")
        if ignored_data:
            parts.append(f"{len(ignored_data)} ignored local data file(s), e.g. {ignored_data[0]}")
        reason = f"uncommitted work ({dirty}: {', '.join(parts)})"
    elif scratch:
        dirty, reason = f"scratch:{len(scratch)}", ""
    else:
        dirty, reason = "clean", ""
    return {
        "dirty": dirty,
        "tracked": tracked[:_MAX_LISTED_PATHS],
        "ignored_data": ignored_data[:_MAX_LISTED_PATHS],
        "scratch": scratch[:_MAX_LISTED_PATHS],
        "nested_repos": nested_repos,
        "hold_reason": reason,
    }


def _classify_remote(wt_path: Path, branch: Optional[str]) -> str:
    if not branch:
        return "detached"
    upstream = _run_git_ro(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"], cwd=wt_path)
    remote_ref = upstream.stdout.strip() if upstream.returncode == 0 else ""
    if not remote_ref:
        candidate = f"refs/remotes/origin/{branch}"
        if _run_git_ro(["rev-parse", "--verify", "--quiet", candidate], cwd=wt_path).returncode == 0:
            remote_ref = candidate
    if not remote_ref:
        return "no-remote"
    ahead = _run_git_ro(["rev-list", "--count", f"{remote_ref}..HEAD"], cwd=wt_path)
    if ahead.returncode != 0 or not ahead.stdout.strip().isdigit():
        return "no-remote"
    count = int(ahead.stdout.strip())
    return "pushed" if count == 0 else f"ahead:{count}"


def _worktree_age_days(wt_path: Path, now_ts: float) -> Optional[float]:
    stamps: list[float] = []
    ct = _run_git_ro(["log", "-1", "--format=%ct"], cwd=wt_path)
    if ct.returncode == 0 and ct.stdout.strip().isdigit():
        stamps.append(float(ct.stdout.strip()))
    git_dir = _run_git_ro(["rev-parse", "--absolute-git-dir"], cwd=wt_path)
    if git_dir.returncode == 0 and git_dir.stdout.strip():
        try:
            stamps.append((Path(git_dir.stdout.strip()) / "index").stat().st_mtime)
        except OSError:
            pass
    if not stamps:
        try:
            stamps.append(wt_path.stat().st_mtime)
        except OSError:
            return None
    return round(max(0.0, now_ts - max(stamps)) / 86400.0, 1)


def _default_kanban_db_path() -> Optional[Path]:
    try:
        return resolve_kanban_db_path()
    except Exception:
        return None


def _resolve_kanban_db(kanban_db_path: Optional[str | Path]) -> Optional[Path]:
    return Path(kanban_db_path) if kanban_db_path else _default_kanban_db_path()


def _query_active_cards(
    kanban_db_path: Optional[Path], *, required: bool = False
) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Read-only list of non-terminal Kanban cards plus an error when ownership cannot be verified.

    A missing DB only means "no cards" when ``required`` is false; destructive callers pass
    ``required=True`` whenever the DB is expected to exist, so a vanished/unreadable DB fails closed.
    """
    if kanban_db_path is None:
        return [], ("cannot resolve kanban.db path" if required else None)
    if not Path(kanban_db_path).is_file():
        if required:
            return [], f"{kanban_db_path} is missing or not a regular file"
        return [], None
    try:
        conn = open_kanban_ro(kanban_db_path)
    except sqlite3.Error as exc:
        return [], f"cannot open {kanban_db_path}: {exc}"
    if conn is None:  # vanished between the check above and the open
        return [], (f"{kanban_db_path} is missing or not a regular file" if required else None)
    try:
        with closing(conn):
            cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
            if not {"id", "status"} <= cols:
                return [], f"{kanban_db_path} has no tasks(id, status) table"
            select = ", ".join(c if c in cols else f"NULL AS {c}" for c in ("id", "status", "workspace_path", "branch_name"))
            placeholders = ", ".join("?" for _ in _INACTIVE_CARD_STATUSES)
            rows = conn.execute(
                f"SELECT {select} FROM tasks WHERE status NOT IN ({placeholders})",
                _INACTIVE_CARD_STATUSES,
            ).fetchall()
            return [dict(r) for r in rows], None
    except sqlite3.Error as exc:
        return [], f"cannot query {kanban_db_path}: {exc}"


def _active_cards(kanban_db_path: Optional[Path]) -> list[dict[str, Any]]:
    return _query_active_cards(kanban_db_path)[0]


def _path_forms(path: str | Path) -> set[Path]:
    """Lexically-normalized absolute path plus its symlink-resolved real path."""
    lexical = Path(os.path.abspath(os.path.expanduser(str(path))))
    forms = {lexical}
    try:
        forms.add(lexical.resolve())
    except (OSError, RuntimeError):
        pass
    return forms


def _is_within(inner: str | Path, outer: str | Path, *, strict: bool = False) -> bool:
    """Whether ``inner`` is (strictly, if asked) inside ``outer``, comparing lexical and real paths."""
    outer_forms, inner_forms = _path_forms(outer), _path_forms(inner)
    if strict and outer_forms & inner_forms:
        return False
    return any(i.is_relative_to(o) for i in inner_forms for o in outer_forms)


def _match_active_card(cards: list[dict[str, Any]], wt_path: Path, branch: Optional[str]) -> Optional[dict[str, Any]]:
    """Active card whose workspace is ``wt_path`` or lies anywhere inside it (else matching branch)."""
    nested: Optional[dict[str, Any]] = None
    for card in cards:
        ws = card.get("workspace_path")
        if ws and _is_within(str(ws), wt_path):
            if not _is_within(str(ws), wt_path, strict=True):
                return card
            nested = nested or card
    if nested is not None:
        return nested
    if branch:
        for card in cards:
            if card.get("branch_name") and str(card["branch_name"]) == branch:
                return card
    return None


_PR_STATE_RANK = {"OPEN": 0, "MERGED": 1, "CLOSED": 2}


def _list_prs_by_branch(full_repo: Optional[str]) -> tuple[dict[str, dict[str, Any]], Optional[str]]:
    if not full_repo:
        return {}, "no GitHub repository known"
    try:
        proc = _run_gh(
            ["pr", "list", "--repo", full_repo, "--state", "all",
             "--json", "number,state,isDraft,headRefName,url", "--limit", "200"],
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {}, f"gh pr list failed: {exc}"
    if proc.returncode != 0:
        return {}, f"gh pr list failed: {(proc.stderr or proc.stdout or '').strip()[:200]}"
    try:
        items = json.loads(proc.stdout or "[]")
    except ValueError:
        return {}, "gh pr list returned invalid JSON"
    by_branch: dict[str, dict[str, Any]] = {}
    for pr in items if isinstance(items, list) else []:
        if not isinstance(pr, dict) or not pr.get("headRefName"):
            continue
        head = str(pr["headRefName"])
        state = str(pr.get("state") or "").upper()
        best = by_branch.get(head)
        key = (_PR_STATE_RANK.get(state, 3), -int(pr.get("number") or 0))
        if best is None or key < (_PR_STATE_RANK.get(str(best.get("state") or "").upper(), 3), -int(best.get("number") or 0)):
            by_branch[head] = pr
    return by_branch, None


def _resolve_base_ref(main_path: Path, default_branch: str, base_ref: Optional[str]) -> Optional[str]:
    if base_ref:
        ref = _validate_git_ref(base_ref, "base_ref")
        if _run_git_ro(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=main_path).returncode == 0:
            return ref
        raise ValueError(f"base_ref {ref!r} does not resolve to a commit in {main_path}")
    for candidate in (f"origin/{default_branch}", default_branch):
        if _run_git_ro(["rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"], cwd=main_path).returncode == 0:
            return candidate
    return None


def _origin_full_repo(main_path: Path) -> Optional[str]:
    proc = _run_git_ro(["remote", "get-url", "origin"], cwd=main_path)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        return normalize_repo(proc.stdout.strip())[0]
    except ValueError:
        return None


def _bucket_for(entry: dict[str, Any]) -> tuple[str, str]:
    dirty = str(entry.get("dirty") or "")
    if dirty.startswith("wip:"):
        return "hold-wip", str(entry.get("dirty_reason") or f"uncommitted work ({dirty})")
    if entry.get("active_card"):
        where = f" via nested workspace {entry['active_card_workspace']}" if entry.get("active_card_workspace") else ""
        return "hold-active-card", f"active card {entry['active_card']} ({entry.get('active_card_status') or 'active'}){where}"
    pr_state = str(entry.get("pr_state") or "").upper()
    if pr_state in ("OPEN", "DRAFT"):
        return "hold-open-pr", f"open PR {entry['pr'].split('/')[0]}"
    nested = entry.get("nested") or []
    if nested:
        more = f" (+{len(nested) - 1} more)" if len(nested) > 1 else ""
        return "review", f"contains nested worktree/repository {nested[0]}{more}; removal would delete it"
    if entry.get("locked"):
        return "review", "worktree is locked"
    if entry.get("prunable") or entry.get("exists") is False:
        return "review", "worktree directory is missing or prunable"
    if dirty == "clean" or dirty.startswith("scratch:"):
        if entry.get("merged") is True:
            return "safe", ""
        if pr_state in ("MERGED", "CLOSED"):
            return "safe", ""
    if entry.get("merged") is None:
        return "review", "merge state unknown (base ref unresolved)"
    return "review", "not merged into base and no merged/closed PR"


def _resolve_audit_targets(repo_spec: Optional[str]) -> list[dict[str, Any]]:
    if repo_spec:
        entry = get_repo(repo_spec)
        if entry is not None:
            return [entry]
        path = Path(repo_spec).expanduser()
        if path.is_dir():
            return [{"slug": path.name, "repo": None, "path": str(path.resolve()), "default_branch": None}]
        raise RepoNotFoundError(f"Repository {repo_spec!r} is not registered and is not a local directory")
    repos = list_repos()
    return [dict(info, slug=info.get("slug") or slug) for slug, info in sorted(repos.items()) if isinstance(info, dict)]


def _audit_one_repo(
    target: dict[str, Any],
    *,
    base_ref: Optional[str],
    cards: list[dict[str, Any]],
    check_pr: bool,
    now_ts: float,
) -> dict[str, Any]:
    main_path = Path(str(target.get("path") or "")).expanduser()
    result: dict[str, Any] = {
        "slug": target.get("slug"),
        "repo": target.get("repo"),
        "path": str(main_path),
        "base_ref": None,
        "worktrees": [],
        "pr_error": None,
        "error": None,
    }
    if not main_path.is_dir():
        result["error"] = f"local checkout not found: {main_path}"
        return result
    listing = _run_git_ro(["worktree", "list", "--porcelain"], cwd=main_path)
    if listing.returncode != 0:
        result["error"] = _git_err(listing, "worktree list")
        return result

    entries = _parse_worktree_porcelain(listing.stdout)
    main_entry = entries[0] if entries else None
    full_repo = target.get("repo") or _origin_full_repo(main_path)
    result["repo"] = full_repo
    default_branch = str(target.get("default_branch") or (main_entry or {}).get("branch") or "main")
    resolved_base = _resolve_base_ref(main_path, default_branch, base_ref)
    result["base_ref"] = resolved_base

    prs: dict[str, dict[str, Any]] = {}
    if check_pr:
        prs, result["pr_error"] = _list_prs_by_branch(full_repo)

    main_resolved = Path(main_entry["path"]).resolve() if main_entry else main_path.resolve()
    for raw in entries[1:]:
        if raw.get("bare"):
            continue
        wt_path = Path(raw["path"])
        if wt_path.exists() and wt_path.resolve() == main_resolved:
            continue
        entry = _inspect_worktree(
            raw, main_path=main_path, full_repo=full_repo, slug=target.get("slug"),
            resolved_base=resolved_base, prs=prs, cards=cards, now_ts=now_ts, registered=entries,
        )
        entry.pop("scratch_paths", None)
        result["worktrees"].append(entry)
    return result


def _inspect_worktree(
    raw: dict[str, Any],
    *,
    main_path: Path,
    full_repo: Optional[str],
    slug: Optional[str],
    resolved_base: Optional[str],
    prs: dict[str, dict[str, Any]],
    cards: list[dict[str, Any]],
    now_ts: float,
    registered: list[dict[str, Any]],
    measure: bool = True,
) -> dict[str, Any]:
    """Classify one ``git worktree list --porcelain`` entry.

    The single source of truth for bucket safety, shared by the audit and the pre-removal
    recheck so the two can never drift. ``measure=False`` skips size/age/remote (informational only).
    ``registered`` is the full live porcelain listing: a worktree that contains any other registered
    worktree (or the main checkout) or a nested repository is never ``safe``, because removing it
    would recursively delete the nested one.
    """
    wt_path = Path(raw["path"])
    branch = raw.get("branch")
    exists = wt_path.is_dir()
    head_sha = raw.get("head_sha")
    entry: dict[str, Any] = {
        "path": str(wt_path),
        "repo": full_repo,
        "slug": slug,
        "branch": branch,
        "head_sha": head_sha,
        "exists": exists,
        "locked": bool(raw.get("locked")),
        "prunable": bool(raw.get("prunable")),
    }
    if measure:
        size = _dir_size_bytes(wt_path) if exists else 0
        entry["size_bytes"] = size
        entry["size_human"] = _human_size(size)
        entry["age_days"] = _worktree_age_days(wt_path, now_ts) if exists else None

    merged: Optional[bool] = None
    if resolved_base and head_sha:
        probe = _run_git_ro(["merge-base", "--is-ancestor", head_sha, resolved_base], cwd=main_path)
        merged = True if probe.returncode == 0 else (False if probe.returncode == 1 else None)
    entry["merged"] = merged
    inventory = _dirty_inventory(wt_path) if exists else {"dirty": "clean", "scratch": [], "hold_reason": ""}
    entry["dirty"] = inventory["dirty"]
    entry["scratch_paths"] = list(inventory.get("scratch") or [])
    if inventory.get("hold_reason"):
        entry["dirty_reason"] = inventory["hold_reason"]
    nested = [
        str(other["path"]) for other in registered
        if other.get("path") and other["path"] != raw["path"] and _is_within(other["path"], wt_path, strict=True)
    ]
    nested += [str(wt_path / rel) for rel in inventory.get("nested_repos") or [] if str(wt_path / rel) not in nested]
    entry["nested"] = nested
    if measure:
        entry["remote"] = _classify_remote(wt_path, branch) if exists else ("detached" if not branch else "no-remote")

    pr = prs.get(branch) if branch else None
    if pr:
        state = str(pr.get("state") or "").upper()
        if state == "OPEN" and pr.get("isDraft"):
            state = "DRAFT"
        entry["pr"] = f"#{pr.get('number')}/{state.lower()}"
        entry["pr_state"] = state
        entry["pr_url"] = pr.get("url")
    else:
        entry["pr"] = "-"
        entry["pr_state"] = None
        entry["pr_url"] = None

    card = _match_active_card(cards, wt_path, branch)
    entry["active_card"] = card.get("id") if card else None
    entry["active_card_status"] = card.get("status") if card else None
    if card and card.get("workspace_path") and _is_within(str(card["workspace_path"]), wt_path, strict=True):
        entry["active_card_workspace"] = str(card["workspace_path"])

    entry["bucket"], entry["hold_reason"] = _bucket_for(entry)
    return entry


def audit_worktrees(
    repo_spec: Optional[str] = None,
    *,
    base_ref: Optional[str] = None,
    kanban_db_path: Optional[str | Path] = None,
    check_pr: bool = True,
) -> dict[str, Any]:
    """Read-only audit of every linked git worktree (never the main checkout).

    Each worktree is classified into a priority-ordered bucket:
    ``hold-wip`` > ``hold-active-card`` > ``hold-open-pr`` > ``safe`` > ``review``.
    """
    targets = _resolve_audit_targets(repo_spec)
    cards = _active_cards(_resolve_kanban_db(kanban_db_path))
    now_ts = time.time()

    repos_out = [
        _audit_one_repo(t, base_ref=base_ref, cards=cards, check_pr=check_pr, now_ts=now_ts) for t in targets
    ]
    worktrees = [wt for r in repos_out for wt in r["worktrees"]]
    by_bucket = {b: 0 for b in WORKTREE_BUCKETS}
    for wt in worktrees:
        by_bucket[wt["bucket"]] += 1
    total = sum(int(wt["size_bytes"]) for wt in worktrees)
    safe_bytes = sum(int(wt["size_bytes"]) for wt in worktrees if wt["bucket"] == "safe")
    return {
        "ok": all(not r.get("error") for r in repos_out),
        "repos": repos_out,
        "worktrees": worktrees,
        "summary": {
            "count": len(worktrees),
            "by_bucket": by_bucket,
            "total_bytes": total,
            "total_human": _human_size(total),
            "safe_bytes": safe_bytes,
            "safe_human": _human_size(safe_bytes),
        },
    }


def _linked_worktree_admin_dir(wt_path: Path, main_path: Path) -> Optional[Path]:
    """Admin dir named by ``wt_path/.git`` if it lies under ``main_path``'s ``worktrees/``; else ``None``."""
    marker = wt_path / ".git"
    if not marker.is_file():
        return None
    try:
        text = marker.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    admin = Path(text[len("gitdir:"):].strip())
    if not admin.is_absolute():
        admin = (wt_path / admin)
    common = _run_git_ro(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=main_path)
    if common.returncode != 0 or not common.stdout.strip():
        return None
    worktrees_root = Path(common.stdout.strip()).resolve() / "worktrees"
    try:
        admin.resolve().relative_to(worktrees_root)
    except ValueError:
        return None
    return admin


def _live_worktree_listing(main_path: Path) -> tuple[list[dict[str, Any]], Optional[str]]:
    listing = _run_git_ro(["worktree", "list", "--porcelain"], cwd=main_path)
    if listing.returncode != 0:
        return [], _git_err(listing, "worktree list")
    return _parse_worktree_porcelain(listing.stdout), None


def _live_worktree_entry(main_path: Path, wt_path_str: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Current porcelain entry for ``wt_path_str`` (``None`` if unregistered) or an error string."""
    entries, err = _live_worktree_listing(main_path)
    if err:
        return None, err
    return next((e for e in entries if e.get("path") == wt_path_str), None), None


def _recheck_before_removal(
    wt: dict[str, Any],
    main_path: Path,
    *,
    repo_out: dict[str, Any],
    prs: dict[str, dict[str, Any]],
    db_path: Optional[Path],
    db_required: bool,
) -> tuple[Optional[tuple[str, str]], list[str], Optional[str]]:
    """Re-verify a ``safe`` candidate immediately before deletion.

    Re-runs the exact audit classifier (``_inspect_worktree`` -> ``_bucket_for``) on the live
    porcelain entry, so lock/prunable/HEAD/merge/dirty/PR/card invariants cannot drift from the
    audit. Returns ``(hold, dropped_scratch, kanban_error)``; ``hold`` is ``(bucket, reason)``.
    Kanban ownership fails closed.
    """
    registered, err = _live_worktree_listing(main_path)
    if err:
        return ("review", f"worktree changed since audit: {err}"), [], None
    current = next((e for e in registered if e.get("path") == wt["path"]), None)
    if current is None:
        return ("review", "worktree changed since audit: no longer registered"), [], None
    if current.get("branch") != wt["branch"] or current.get("head_sha") != wt["head_sha"]:
        return ("review", "worktree changed since audit: branch or HEAD moved"), [], None
    if current.get("locked"):
        return ("review", "worktree is locked"), [], None

    cards, kanban_err = _query_active_cards(db_path, required=db_required)
    if kanban_err:
        return (
            ("hold-active-card", f"active-card ownership could not be verified (fail closed): {kanban_err}"),
            [],
            kanban_err,
        )
    fresh = _inspect_worktree(
        current, main_path=main_path, full_repo=repo_out.get("repo"), slug=repo_out.get("slug"),
        resolved_base=repo_out.get("base_ref"), prs=prs, cards=cards, now_ts=time.time(),
        registered=registered, measure=False,
    )
    if fresh["bucket"] != "safe":
        return (fresh["bucket"], f"changed since audit: {fresh['hold_reason']}"), [], None
    return None, list(fresh["scratch_paths"]), None


def _is_lock_refusal(proc: subprocess.CompletedProcess[str]) -> bool:
    text = f"{proc.stderr or ''}\n{proc.stdout or ''}".lower()
    return "locked" in text or "lock reason" in text


def prune_safe_worktrees(
    repo_spec: Optional[str] = None,
    *,
    base_ref: Optional[str] = None,
    kanban_db_path: Optional[str | Path] = None,
    check_pr: bool = True,
) -> dict[str, Any]:
    """Remove ONLY worktrees that are ``safe`` both in the audit and on a live re-check.

    Fail-closed rules:
    - Kanban ownership is *required* when ``kanban_db_path`` is explicit, when the default DB path
      cannot be resolved, or once the default DB has been seen to exist (before the audit or at any
      later candidate). A required DB that is missing/non-file/unreadable holds every candidate.
    - ``shutil.rmtree`` never overrides a ``git worktree remove`` refusal: it only runs when git has
      already de-registered the worktree (so no lock can remain), its admin dir is gone, the output
      carries no lock refusal, and Kanban ownership re-verifies.
    """
    db_path = _resolve_kanban_db(kanban_db_path)
    db_required = bool(kanban_db_path) or db_path is None or db_path.exists()
    audit = audit_worktrees(repo_spec, base_ref=base_ref, kanban_db_path=kanban_db_path, check_pr=check_pr)
    kanban_errors: list[str] = []
    removed: list[dict[str, Any]] = []
    held: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    def hold(wt: dict[str, Any], bucket: str, reason: str) -> None:
        held.append({
            "path": wt["path"], "branch": wt["branch"], "bucket": bucket,
            "hold_reason": reason, "size_bytes": wt["size_bytes"],
        })

    def note_kanban_error(err: Optional[str]) -> None:
        if err and err not in kanban_errors:
            kanban_errors.append(err)

    for repo_out in audit["repos"]:
        main_path = Path(repo_out["path"])
        candidates = [wt for wt in repo_out["worktrees"] if wt["bucket"] == "safe"]
        for wt in repo_out["worktrees"]:
            if wt["bucket"] != "safe":
                hold(wt, wt["bucket"], wt["hold_reason"])
        if not candidates:
            continue
        # PR state is re-read too; a lookup failure only demotes closed-PR candidates to review.
        prs = _list_prs_by_branch(repo_out.get("repo"))[0] if check_pr else {}
        touched = False
        for wt in candidates:
            wt_path = Path(wt["path"])
            if db_path is not None and db_path.exists():
                db_required = True
            recheck = dict(repo_out=repo_out, prs=prs, db_path=db_path, db_required=db_required)
            held_as, dropped_scratch, kanban_error = _recheck_before_removal(wt, main_path, **recheck)
            note_kanban_error(kanban_error)
            if held_as is not None:
                hold(wt, *held_as)
                continue

            touched = True
            proc = _run_git(["worktree", "remove", "--force", str(wt_path)], cwd=main_path, timeout=120)
            method = "git worktree remove"
            if proc.returncode != 0 or wt_path.exists():
                if _is_lock_refusal(proc):
                    hold(wt, "review", "worktree is locked (git refused removal)")
                    continue
                current, list_err = _live_worktree_entry(main_path, wt["path"])
                if list_err or current is not None:
                    # Still registered: git refused. Report why, never override it.
                    held_as, _, kanban_error = (
                        (None, [], None) if list_err else _recheck_before_removal(wt, main_path, **recheck)
                    )
                    note_kanban_error(kanban_error)
                    if held_as is not None:
                        hold(wt, *held_as)
                    else:
                        errors.append({"path": wt["path"], "error": list_err or _git_err(proc, "worktree remove")})
                    continue
                if wt_path.exists():
                    refusal = _fallback_refusal(wt_path, main_path, wt["branch"], db_path, db_required)
                    if refusal is not None:
                        note_kanban_error(refusal[2])
                        if refusal[0] is not None:
                            hold(wt, *refusal[0])
                        else:
                            errors.append({"path": wt["path"], "error": f"{_git_err(proc, 'worktree remove')}; {refusal[1]}"})
                        continue
                    shutil.rmtree(wt_path, ignore_errors=True)
                    method = "fallback rmtree"
                    if wt_path.exists():
                        errors.append({"path": wt["path"], "error": _git_err(proc, "worktree remove")})
                        continue
            removed.append({
                "path": wt["path"], "branch": wt["branch"], "size_bytes": wt["size_bytes"],
                "size_human": wt["size_human"], "method": method, "dropped_scratch": dropped_scratch,
            })
        if touched:
            _run_git(["worktree", "prune"], cwd=main_path)

    reclaimed = sum(int(r["size_bytes"]) for r in removed)
    return {
        "ok": audit["ok"] and not errors and not kanban_errors,
        "removed": removed,
        "held": held,
        "errors": errors,
        "kanban_errors": kanban_errors,
        "reclaimed_bytes": reclaimed,
        "reclaimed_human": _human_size(reclaimed),
        "audit": audit["summary"],
    }


def _fallback_refusal(
    wt_path: Path, main_path: Path, branch: Optional[str], db_path: Optional[Path], db_required: bool
) -> Optional[tuple[Optional[tuple[str, str]], str, Optional[str]]]:
    """Why leftover files of a git-de-registered worktree must NOT be rmtree'd, or ``None`` if allowed.

    Returns ``(hold, error_text, kanban_error)``.
    """
    wt_resolved, main_resolved = wt_path.resolve(), main_path.resolve()
    if wt_resolved == main_resolved or main_resolved.is_relative_to(wt_resolved):
        return None, "refusing rmtree: path is or contains the main checkout", None
    admin = _linked_worktree_admin_dir(wt_path, main_path)
    if admin is None:
        return None, "refusing rmtree: not a linked worktree of this repository", None
    if admin.exists():
        # Admin dir (and any lock file in it) still present: git did not release this worktree.
        return None, f"refusing rmtree: worktree admin dir still exists ({admin})", None
    registered, list_err = _live_worktree_listing(main_path)
    if list_err:
        return None, f"refusing rmtree: {list_err}", None
    for other in registered:
        if other.get("path") and _is_within(other["path"], wt_path, strict=True):
            return (("review", f"contains nested worktree {other['path']}; removal would delete it"), "", None)
    for root, dirs, files in os.walk(wt_path):
        if Path(root) != wt_path and (".git" in dirs or ".git" in files):
            return (("review", f"contains nested repository {root}; removal would delete it"), "", None)
    cards, kanban_err = _query_active_cards(db_path, required=db_required)
    if kanban_err:
        return (
            ("hold-active-card", f"active-card ownership could not be verified (fail closed): {kanban_err}"),
            "", kanban_err,
        )
    card = _match_active_card(cards, wt_path, branch)
    if card:
        return (("hold-active-card", f"active card {card.get('id')} claimed leftover directory"), "", None)
    return None
