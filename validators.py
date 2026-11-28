"""Layer 0 input validators and identifier normalizers (stdlib + `vocabulary` only).

Each validator raises ``ValueError`` with a caller-facing message; adapters (CLI, tools, HTTP) map
it to their own error surface. Every SHA check in Factonio goes through `FULL_SHA_RE`.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .vocabulary import contains_holdout_reference

# A full commit id: SHA-1 (40 hex) or SHA-256 (64 hex). Callers lowercase before storing.
FULL_SHA_RE = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")
# Lowercase-only alternation for card stamps the orchestrator writes from a validated (lowercased) SHA.
FULL_SHA_LOWER_PATTERN = r"[0-9a-f]{40}|[0-9a-f]{64}"
SLUG_RE = re.compile(r"[^a-z0-9-]+")
SAFE_TENANT_RE = SLUG_RE
TENANT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
ARENA_ID_RE = re.compile(r"^ar_[0-9a-f]{6,32}$")

_GITHUB_URL_RE = re.compile(
    r"^(?:https?://github\.com/|git@github\.com:)([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)
OWNER_REPO_RE = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]+")

RunGit = Callable[..., Any]  # (args, *, cwd) -> CompletedProcess


def normalize_repo(repo_spec: str) -> tuple[str, str]:
    """Normalize `owner/repo` or GitHub URL into (`owner/repo`, `slug`)."""
    raw = (repo_spec or "").strip()
    m = _GITHUB_URL_RE.match(raw) or OWNER_REPO_RE.match(raw)
    if not m:
        raise ValueError(
            f"Invalid GitHub repository {repo_spec!r}; expected 'owner/repo' or a GitHub HTTPS/SSH URL."
        )
    owner, name = m.group(1), m.group(2)
    full = f"{owner}/{name}"
    slug = SLUG_RE.sub("-", f"{owner}-{name}".lower()).strip("-")[:64]
    return full, slug


def make_tenant(repo_slug: str, *, issue_number: Optional[int] = None, label: Optional[str] = None) -> str:
    """Generate a clean kebab-case Kanban tenant slug for a workstream."""
    base = SLUG_RE.sub("-", repo_slug.lower()).strip("-")
    if issue_number is not None:
        return f"{base}-i{int(issue_number)}"[:64]
    if label:
        tail = SLUG_RE.sub("-", label.lower()).strip("-")[:24]
        if tail:
            return f"{base}-{tail}"[:64]
    return f"{base}-{int(time.time()) % 100000}"[:64]


def safe_tenant_dirname(tenant: Optional[str]) -> str:
    """One safe path component for a tenant's run directory (``default`` when empty)."""
    return SAFE_TENANT_RE.sub("-", (tenant or "default").lower()).strip("-") or "default"


def single_line(text: Any) -> str:
    """Collapse newlines/control characters to single spaces so a value fits one Markdown line."""
    return " ".join(_CONTROL_CHARS_RE.sub(" ", str(text or "")).split())


def stamp_token(value: Any, default: str) -> str:
    """Single-line, backtick-free rendering of a stamp field so it cannot break the panel stamp parser."""
    return single_line(str(value or "").replace("`", "'")) or default


def require_clean_str(value: Any, field: str, *, optional: bool = False, non_empty: bool = False) -> None:
    """The one preflight check for every caller string that can reach git, gh, a UTF-8 file or trail.db."""
    if value is None and optional:
        return
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string; got {type(value).__name__}")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError(f"{field} is not valid UTF-8 (contains unencodable characters)") from None
    if "\x00" in value:
        raise ValueError(f"{field} must not contain NUL (\\x00) characters")
    if non_empty and not value.strip():
        raise ValueError(f"{field} must be a non-empty string")


def is_full_sha(value: Any) -> bool:
    """True for a full 40/64-hex commit id (any case, no surrounding whitespace)."""
    return isinstance(value, str) and FULL_SHA_RE.fullmatch(value) is not None


def validate_full_sha(value: Any, field: str = "expected_head_sha") -> str:
    """Accept only a full 40/64-hex commit id (lowercased); abbreviations and refs are refused."""
    sha = value.strip().lower() if isinstance(value, str) else ""
    if not FULL_SHA_RE.fullmatch(sha):
        raise ValueError(f"{field} must be a full 40- or 64-character hex commit SHA; got {value!r}")
    return sha


def validate_pr_number(value: Any, field: str = "pr_number") -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive integer; got {value!r}")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a positive integer; got {value!r}") from None
    if number < 1 or (isinstance(value, float) and value != number):
        raise ValueError(f"{field} must be a positive integer; got {value!r}")
    return number


def validate_default_branch(value: Any) -> str:
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


def validate_branch_field(value: Any, field: str) -> str:
    try:
        return validate_default_branch(value)
    except ValueError as exc:
        raise ValueError(str(exc).replace("default_branch", field, 1)) from None


def validate_git_ref_name(cwd: Path, name: str, field: str, *, run_git: RunGit) -> None:
    """Refuse anything git would not accept as a plain branch name (``@``, ``main~1``, ``@{-1}``, ``HEAD``...).

    ``run_git(args, cwd=...)`` is injected (usually `git_cli.run_git`) so this module stays git-free.
    """
    if name == "@":
        raise ValueError(f"{field} {name!r} is not a valid branch name")
    for args in (["check-ref-format", f"refs/heads/{name}"], ["check-ref-format", "--branch", name]):
        if run_git(args, cwd=cwd).returncode != 0:
            raise ValueError(f"{field} {name!r} is not a valid branch name")
    if name == "HEAD":
        raise ValueError(f"{field} {name!r} is not a valid branch name")


def validate_tenant_slug(value: Any, field: str = "tenant") -> str:
    """A workstream tenant usable as one path component: no separators, ``..``, controls or holdout."""
    if (
        not isinstance(value, str)
        or not TENANT_RE.fullmatch(value)
        or ".." in value
        or contains_holdout_reference(value)
    ):
        raise ValueError(f"{field} must match ^[A-Za-z0-9][A-Za-z0-9._-]{{0,127}}$ (no '..').")
    return value
