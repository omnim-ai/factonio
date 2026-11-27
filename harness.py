"""Repo-local verification harness: `<repo>/.factonio/verify.md` + `.factonio/harness.json`.

Pure stdlib + the `git` CLI. Detects a repository's stack and black-box surfaces
(HTTP routes, CLI commands), bootstraps a versioned Launch / Doctor / Drive /
Evidence / Cleanup playbook, validates and shows it, runs optional repo-defined
hooks, proves a freshly generated harness (`prove`), detects loosened gates (including regenerated or
tampered frozen `visual_diff` baselines), tracks formal-invariant drift (`covers` / `sync_commit`), classifies
drift (`doc_drift` / `harness_gap` / `product_regression`), and audits drift read-only (every git probe uses
`--no-optional-locks`).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, NamedTuple, Optional

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11: TOML manifests are skipped
    tomllib = None  # type: ignore[assignment]

HARNESS_DIR_NAME = ".factonio"
VERIFY_MD_NAME = "verify.md"
HARNESS_JSON_NAME = "harness.json"
HARNESS_SCHEMA_VERSION = 1
REQUIRED_SECTIONS: tuple[str, ...] = ("Launch", "Doctor", "Drive", "Evidence", "Cleanup")
EXTRA_SECTIONS: tuple[str, ...] = ("Feature Map", "Hooks")
HOOK_KINDS: tuple[str, ...] = ("visual_diff", "invariant", "custom")
PHASE_KEYS: tuple[str, ...] = tuple(s.lower() for s in REQUIRED_SECTIONS)

MAX_FILE_BYTES = 256 * 1024
MAX_SCAN_FILES = 4000
MAX_DEPTH = 8
MAX_OUTPUT_CHARS = 4000
DEFAULT_HOOK_TIMEOUT = 120

SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", HARNESS_DIR_NAME, ".worktrees", "node_modules", ".venv", "venv", "env",
    "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache", ".tox", ".nox", "dist", "build",
    "target", "vendor", ".next", ".nuxt", "coverage", ".cache", "site-packages",
})
# Top-level test directories: their files feed the `tests` feature but never route/CLI discovery.
_TEST_DIRS = frozenset({"tests", "test", "spec", "__tests__"})
SCAN_EXTS = frozenset({".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rs", ".rb"})
SOURCE_EXTS = SCAN_EXTS | frozenset({
    ".java", ".kt", ".kts", ".swift", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".php", ".sh",
    ".vue", ".svelte", ".scala", ".ex", ".exs", ".lua", ".dart",
})
_LANG_BY_EXT = {
    ".py": "python", ".js": "node", ".mjs": "node", ".cjs": "node", ".ts": "node", ".tsx": "node",
    ".jsx": "node", ".go": "go", ".rs": "rust", ".rb": "ruby",
}

_HTTP_VERBS = "get|post|put|patch|delete|head|options"
_PY_ROUTE_RE = re.compile(
    rf"@\s*[A-Za-z_][\w.]*\.({_HTTP_VERBS})\(\s*[rbuf]?[\"']([^\"']+)[\"']", re.IGNORECASE
)
_PY_FLASK_RE = re.compile(
    r"@\s*[A-Za-z_][\w.]*\.(?:route|api_route)\(\s*[rbuf]?[\"']([^\"']+)[\"']([^)]*)\)"
)
_JS_ROUTE_RE = re.compile(
    rf"\b(?:app|router|server|api|fastify|route[r]?s?)\.({_HTTP_VERBS})\(\s*[\"'`]([^\"'`]+)[\"'`]",
    re.IGNORECASE,
)
_GO_ROUTE_RE = re.compile(r"\b(?:HandleFunc|Handle)\(\s*\"([^\"]+)\"")
_RUBY_ROUTE_RE = re.compile(rf"^\s*({_HTTP_VERBS})\s+[\"']([^\"']+)[\"']", re.MULTILINE)
_METHODS_RE = re.compile(r"methods\s*=\s*[\[(]([^\])]*)[\])]")
_ADD_PARSER_RE = re.compile(r"\.add_parser\(\s*[rbuf]?[\"']([^\"']+)[\"']")
_CLICK_RE = re.compile(r"@\s*[A-Za-z_][\w.]*\.command\(\s*(?:name\s*=\s*)?[\"']([^\"']+)[\"']")
_MAKE_TARGET_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)\s*:(?!=)", re.MULTILINE)
_WORKFLOW_RUN_RE = re.compile(r"^[ \t]*-?[ \t]*run:[ \t]*(?![|>\s])(.+?)[ \t]*$", re.MULTILINE)
# Allowlist of CI `run:` commands suitable for black-box Drive verification.
# A positive allowlist prevents provisioning, package-manager, privileged, and
# shell-boilerplate steps from leaking into generated verification plans.
_CI_VERIFY_RE = re.compile(
    r"^(?:(?:[A-Z_][A-Z0-9_]*=\S+\s+)|(?:env(?:\s+-u\s+\S+)*\s+))*"
    r"(?:pytest\b|"
    r"python3?(?:\s+-W\s+\S+)*(?:\s+-m\s+(?:pytest|unittest|compileall|mypy|flake8|ruff|black|isort|pylint|doctest)\b|\s+(?:tools|scripts|tests)/)|"
    r"uv\s+run\b|uvx\b|ruff\b|mypy\b|pyright\b|flake8\b|pylint\b|black\b|isort\b|"
    r"npm\s+(?:test|run)\b|npx\b|pnpm\s+(?:test|run|exec)\b|yarn\s+(?:test|run)\b|bun\s+(?:test|run|x)\b|"
    r"node\s+(?:--check|--test)\b|vitest\b|jest\b|playwright\b|eslint\b|tsc\b|prettier\b|"
    r"cargo\s+(?:test|check|clippy|fmt|build)\b|go\s+(?:test|vet|build)\b|"
    r"make(?:\s+(?:test|check|lint|verify|build))?\b|just\b|ctest\b|"
    r"mvn\b|\./gradlew\b|gradle\b|dotnet\s+(?:test|build|format)\b|"
    r"bundle\s+exec\b|rspec\b|mix\s+(?:test|format|credo)\b|phpunit\b|shellcheck\b|"
    r"\./(?:scripts|tools|bin)/)",
    re.IGNORECASE,
)
_CI_SETUP_SUBCMD_RE = re.compile(
    r"(?:^|\s)(?:pip\s+install|npm\s+(?:ci|install)|uv\s+sync)\b",
    re.IGNORECASE,
)


def _is_ci_verification_command(cmd: str) -> bool:
    """Return True only for verification/test/lint/build CI steps."""
    text = cmd.strip()
    if not text or not _CI_VERIFY_RE.match(text):
        return False
    return _CI_SETUP_SUBCMD_RE.search(text) is None
_GITHUB_REMOTE_RE = re.compile(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")
_H2_RE = re.compile(r"^##\s+(.+?)\s*#*\s*$", re.MULTILINE)
_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_HOOK_LINE_RE = re.compile(
    r"^\s*[-*]\s+\*\*(?P<name>[^*]+)\*\*\s*\(`?(?P<kind>[a-z_]+)`?(?:\s*,\s*(?P<opt>optional|required))?\)\s*:\s*`(?P<cmd>[^`]+)`",
    re.MULTILINE,
)
_ID_RE = re.compile(r"[^a-z0-9]+")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_ro(repo_path: Path, args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    """Read-only git probe: never refreshes/rewrites the index or takes optional locks."""
    try:
        return subprocess.run(
            ["git", "--no-optional-locks", *args],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(["git", *args], 128, "", str(exc))


def _is_git_repo(repo_path: Path) -> bool:
    proc = _git_ro(repo_path, ["rev-parse", "--is-inside-work-tree"])
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def _head_commit(repo_path: Path) -> Optional[str]:
    proc = _git_ro(repo_path, ["rev-parse", "--verify", "-q", "HEAD^{commit}"])
    sha = proc.stdout.strip()
    return sha if proc.returncode == 0 and sha else None


def _resolve_commit(repo_path: Path, ref: str) -> Optional[str]:
    proc = _git_ro(repo_path, ["rev-parse", "--verify", "-q", f"{ref}^{{commit}}"])
    sha = proc.stdout.strip()
    return sha if proc.returncode == 0 and sha else None


def _validate_ref(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"base_ref must be a non-empty git revision; got {value!r}")
    ref = value.strip()
    if ref.startswith("-") or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in ref):
        raise ValueError(f"base_ref must not start with '-' or contain whitespace/control characters; got {value!r}")
    return ref


def _read_text(path: Path, limit: int = MAX_FILE_BYTES) -> str:
    try:
        with path.open("rb") as fh:
            data = fh.read(limit)
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def _tail(text: Optional[str], limit: int = MAX_OUTPUT_CHARS) -> str:
    text = text or ""
    return text if len(text) <= limit else "..." + text[-limit:]


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _slug_id(value: str) -> str:
    return _ID_RE.sub("-", value.lower()).strip("-") or "root"


def _unique_id(base: str, taken: set[str]) -> str:
    fid, n = base, 2
    while fid in taken:
        fid, n = f"{base}-{n}", n + 1
    taken.add(fid)
    return fid


def _is_str_list(value: Any, *, non_empty_items: bool = False) -> bool:
    return isinstance(value, list) and all(
        isinstance(v, str) and (v.strip() or not non_empty_items) for v in value
    )


def _str_list(value: Any) -> list[str]:
    """The string items of ``value`` when it is a list; ``[]`` for any other type."""
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _harness_paths(repo_path: Path) -> tuple[Path, Path, Path]:
    hdir = repo_path / HARNESS_DIR_NAME
    return hdir, hdir / VERIFY_MD_NAME, hdir / HARNESS_JSON_NAME


def _origin_full_repo(repo_path: Path) -> Optional[str]:
    proc = _git_ro(repo_path, ["remote", "get-url", "origin"])
    m = _GITHUB_REMOTE_RE.search(proc.stdout.strip()) if proc.returncode == 0 else None
    return f"{m.group(1)}/{m.group(2)}" if m else None


def _resolve_repo(repo_spec: Any) -> dict[str, Any]:
    """Resolve a registered repo (slug / owner/repo) or a local directory path."""
    spec = str(repo_spec or "").strip()
    if not spec:
        raise ValueError("repo is required (registered slug, 'owner/repo', or a local path)")
    try:
        from .config import get_repo

        entry = get_repo(spec)
    except Exception:
        entry = None
    if entry and entry.get("path"):
        path = Path(str(entry["path"])).expanduser()
        if not path.is_dir():
            raise ValueError(f"Registered repo {spec!r} has no local checkout at {path}")
        return {"path": path.resolve(), "repo": str(entry.get("repo") or spec), "slug": str(entry.get("slug") or "")}
    path = Path(spec).expanduser()
    if path.is_dir():
        path = path.resolve()
        return {"path": path, "repo": _origin_full_repo(path) or path.name, "slug": ""}
    raise ValueError(
        f"Repository {spec!r} is neither registered with Factonio nor an existing local directory."
    )


# ---------------------------------------------------------------------------
# Glob matching (`**` aware)
# ---------------------------------------------------------------------------


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    pat = pattern.strip()
    if pat.startswith("./"):
        pat = pat[2:]
    out: list[str] = []
    i = 0
    while i < len(pat):
        ch = pat[i]
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pat.startswith("**", i):
            out.append(".*")
            i += 2
        elif ch == "*":
            out.append("[^/]*")
            i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(ch))
            i += 1
    if pat.endswith("/"):
        out.append(".*")
    return re.compile("^" + "".join(out) + "$")


def glob_matches(pattern: str, rel_path: str) -> bool:
    """True when a POSIX-relative path matches a feature-map glob (`*`, `?`, `**`)."""
    return bool(_glob_to_regex(pattern).match(rel_path.replace(os.sep, "/")))


def _any_match(patterns: Iterable[str], rel_path: str) -> bool:
    return any(glob_matches(p, rel_path) for p in patterns)


# ---------------------------------------------------------------------------
# Repository profile detection
# ---------------------------------------------------------------------------


def _walk_files(repo_path: Path, skip_dirs: frozenset[str] = SKIP_DIRS) -> tuple[list[str], bool]:
    """Bounded walk returning POSIX relative paths (skipping vendored/cache dirs)."""
    files: list[str] = []
    truncated = False
    root_depth = len(repo_path.parts)
    for dirpath, dirnames, filenames in os.walk(repo_path):
        depth = len(Path(dirpath).parts) - root_depth
        dirnames[:] = sorted(
            d for d in dirnames if d not in skip_dirs and not d.endswith(".egg-info") and depth < MAX_DEPTH
        )
        for name in sorted(filenames):
            rel = (Path(dirpath) / name).relative_to(repo_path).as_posix()
            files.append(rel)
            if len(files) >= MAX_SCAN_FILES:
                return files, True
    return files, truncated


def _list_repo_files(repo_path: Path, skip_dirs: frozenset[str] = SKIP_DIRS) -> tuple[list[str], bool]:
    """Tracked + untracked (non-ignored) files via read-only git, else a bounded walk."""
    if _is_git_repo(repo_path):
        proc = _git_ro(repo_path, ["ls-files", "-z", "--cached", "--others", "--exclude-standard"], timeout=60)
        if proc.returncode == 0:
            seen: set[str] = set()
            files: list[str] = []
            for rel in proc.stdout.split("\0"):
                if not rel or rel in seen:
                    continue
                if any(part in skip_dirs for part in rel.split("/")[:-1]):
                    continue
                if not (repo_path / rel).exists():
                    continue
                seen.add(rel)
                files.append(rel)
            files.sort()
            truncated = len(files) > MAX_SCAN_FILES
            return files[:MAX_SCAN_FILES], truncated
    return _walk_files(repo_path, skip_dirs)


# Formal invariants may cover the harness's own files (e.g. `.factonio/model.cfg`), so their inventory keeps
# `.factonio/` while every other skipped directory (`.git/`, venvs, build output) stays excluded.
_INVARIANT_SKIP_DIRS = SKIP_DIRS - {HARNESS_DIR_NAME}


def _invariant_files(root: Path) -> list[str]:
    return _list_repo_files(root, _INVARIANT_SKIP_DIRS)[0]


def _load_toml(path: Path) -> dict[str, Any]:
    if tomllib is None or not path.is_file():
        return {}
    try:
        return tomllib.loads(_read_text(path))
    except Exception:
        return {}


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(_read_text(path))
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _routes_in(text: str, ext: str) -> list[str]:
    routes: list[str] = []
    if ext == ".py":
        for verb, path in _PY_ROUTE_RE.findall(text):
            routes.append(f"{verb.upper()} {path}")
        for path, rest in _PY_FLASK_RE.findall(text):
            m = _METHODS_RE.search(rest)
            methods = re.findall(r"[\"']([A-Za-z]+)[\"']", m.group(1)) if m else ["GET"]
            routes.extend(f"{meth.upper()} {path}" for meth in methods)
    elif ext in {".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx"}:
        for verb, path in _JS_ROUTE_RE.findall(text):
            if path.startswith("/"):
                routes.append(f"{verb.upper()} {path}")
    elif ext == ".go":
        for pattern in _GO_ROUTE_RE.findall(text):
            routes.append(pattern if " " in pattern else f"ANY {pattern}")
    elif ext == ".rb":
        for verb, path in _RUBY_ROUTE_RE.findall(text):
            routes.append(f"{verb.upper()} {path}")
    return routes


def _cli_in(text: str, ext: str) -> list[str]:
    if ext != ".py":
        return []
    return [*_ADD_PARSER_RE.findall(text), *_CLICK_RE.findall(text)]


def detect_repo_profile(repo_path: str | Path) -> dict[str, Any]:
    """Detect stack, manifests, entrypoints, CI commands, HTTP routes and CLI commands (stdlib only)."""
    root = Path(repo_path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Repository path {root} is not a directory")
    files, truncated = _list_repo_files(root)
    file_set = set(files)

    stack: list[str] = []
    manifests: list[str] = []
    entrypoints: list[str] = []
    cli_commands: dict[str, str] = {}
    routes: dict[str, str] = {}
    commands: dict[str, list[str]] = {"setup": [], "build": [], "test": [], "lint": [], "start": []}

    def add_stack(name: str) -> None:
        if name not in stack:
            stack.append(name)

    def add_cmd(kind: str, cmd: str) -> None:
        if cmd and cmd not in commands[kind]:
            commands[kind].append(cmd)

    pyproject = _load_toml(root / "pyproject.toml") if "pyproject.toml" in file_set else {}
    if "pyproject.toml" in file_set:
        manifests.append("pyproject.toml")
        entrypoints.append("pyproject.toml")
        add_stack("python")
        project = _as_dict(pyproject.get("project"))
        for name in _as_dict(project.get("scripts")):
            cli_commands.setdefault(str(name), "pyproject.toml")
        poetry = _as_dict(_as_dict(pyproject.get("tool")).get("poetry"))
        for name in _as_dict(poetry.get("scripts")):
            cli_commands.setdefault(str(name), "pyproject.toml")
        add_cmd("setup", "python3 -m pip install -e .")
    for name in ("setup.py", "setup.cfg", "requirements.txt"):
        if name in file_set:
            manifests.append(name)
            add_stack("python")

    if "package.json" in file_set:
        pkg = _load_json(root / "package.json")
        manifests.append("package.json")
        entrypoints.append("package.json")
        add_stack("node")
        bin_field = pkg.get("bin")
        if isinstance(bin_field, str) and pkg.get("name"):
            cli_commands.setdefault(str(pkg["name"]).split("/")[-1], "package.json")
        elif isinstance(bin_field, dict):
            for name in bin_field:
                cli_commands.setdefault(str(name), "package.json")
        scripts = _as_dict(pkg.get("scripts"))
        add_cmd("setup", "npm ci" if "package-lock.json" in file_set else "npm install")
        for script, kind in (("build", "build"), ("test", "test"), ("lint", "lint"), ("start", "start"), ("dev", "start")):
            if script in scripts:
                add_cmd(kind, "npm test" if script == "test" else ("npm start" if script == "start" else f"npm run {script}"))
        if isinstance(pkg.get("main"), str):
            entrypoints.append(str(pkg["main"]))

    if "Cargo.toml" in file_set:
        manifests.append("Cargo.toml")
        entrypoints.append("Cargo.toml")
        add_stack("rust")
        cargo = _load_toml(root / "Cargo.toml")
        bins = cargo.get("bin")
        bins = bins if isinstance(bins, list) else []
        for b in bins:
            if isinstance(b, dict) and b.get("name"):
                cli_commands.setdefault(str(b["name"]), "Cargo.toml")
        if not bins and "src/main.rs" in file_set and isinstance(cargo.get("package"), dict):
            name = cargo["package"].get("name")
            if name:
                cli_commands.setdefault(str(name), "Cargo.toml")
        add_cmd("build", "cargo build")
        add_cmd("test", "cargo test")

    if "go.mod" in file_set:
        manifests.append("go.mod")
        entrypoints.append("go.mod")
        add_stack("go")
        add_cmd("build", "go build ./...")
        add_cmd("test", "go test ./...")

    if "Gemfile" in file_set:
        manifests.append("Gemfile")
        add_stack("ruby")

    if "Makefile" in file_set:
        manifests.append("Makefile")
        entrypoints.append("Makefile")
        add_stack("make")
        targets = set(_MAKE_TARGET_RE.findall(_read_text(root / "Makefile")))
        for target, kind in (("build", "build"), ("test", "test"), ("check", "test"), ("lint", "lint"), ("run", "start")):
            if target in targets:
                add_cmd(kind, f"make {target}")

    if "plugin.yaml" in file_set:
        manifests.append("plugin.yaml")
        entrypoints.append("plugin.yaml")
        add_stack("hermes-plugin")

    workflows = sorted(f for f in files if re.match(r"^\.github/workflows/[^/]+\.ya?ml$", f))
    ci_commands: list[str] = []
    for wf in workflows:
        for cmd in _WORKFLOW_RUN_RE.findall(_read_text(root / wf)):
            cmd = cmd.strip()
            if len(cmd) >= 2 and cmd[0] == cmd[-1] and cmd[0] in "\"'":
                cmd = cmd[1:-1].strip()
            if cmd and "${{" not in cmd and cmd not in ci_commands and len(ci_commands) < 25:
                ci_commands.append(cmd)
    if workflows:
        add_stack("github-actions")

    source_files: list[str] = []
    for rel in files:
        ext = Path(rel).suffix.lower()
        if ext in SOURCE_EXTS:
            source_files.append(rel)
        if ext in _LANG_BY_EXT:
            add_stack(_LANG_BY_EXT[ext])
        if ext not in SCAN_EXTS or ("/" in rel and rel.split("/", 1)[0] in _TEST_DIRS):
            continue
        text = _read_text(root / rel)
        if not text:
            continue
        for route in _routes_in(text, ext):
            routes.setdefault(route, rel)
        for cmd in _cli_in(text, ext):
            cli_commands.setdefault(cmd, rel)

    if "python" in stack and not commands["test"]:
        if any(f.startswith("tests/") for f in files):
            uses_pytest = "pytest" in json.dumps(pyproject) or any(
                f.endswith(("pytest.ini", "conftest.py")) for f in files
            )
            add_cmd("test", "python3 -m pytest -q" if uses_pytest else "python3 -m unittest discover -s tests -q")
        add_cmd("build", "python3 -W error -m compileall -q .")
    for name in ("__main__.py", "main.py", "app.py", "manage.py", "cli.py", "server.py", "index.js", "main.go"):
        for rel in files:
            if rel == name or rel.endswith("/" + name):
                if rel.count("/") <= 2 and rel not in entrypoints:
                    entrypoints.append(rel)

    return {
        "repo_path": str(root),
        "stack": stack,
        "manifests": manifests,
        "entrypoints": entrypoints,
        "workflows": workflows,
        "ci_commands": ci_commands,
        "commands": commands,
        "routes": sorted(routes),
        "route_sources": dict(sorted(routes.items())),
        "cli_commands": sorted(cli_commands),
        "cli_sources": dict(sorted(cli_commands.items())),
        "source_files": source_files,
        "files_scanned": len(files),
        "truncated": truncated,
    }


# ---------------------------------------------------------------------------
# Hook validation
# ---------------------------------------------------------------------------


def validate_hooks(hooks: Any) -> list[dict[str, Any]]:
    """Validate and normalize hook declarations; raises ``ValueError`` on any invalid entry."""
    if hooks is None:
        return []
    if isinstance(hooks, str):
        try:
            hooks = json.loads(hooks)
        except ValueError as exc:
            raise ValueError(f"hooks must be a JSON list of objects: {exc}") from exc
    if not isinstance(hooks, list):
        raise ValueError("hooks must be a list of {name, kind, command, optional} objects")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idx, raw in enumerate(hooks):
        if not isinstance(raw, dict):
            raise ValueError(f"hooks[{idx}] must be an object")
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ValueError(f"hooks[{idx}].name is required")
        if name in seen:
            raise ValueError(f"duplicate hook name {name!r}")
        kind = str(raw.get("kind") or "custom").strip()
        if kind not in HOOK_KINDS:
            raise ValueError(f"hooks[{idx}].kind must be one of {', '.join(HOOK_KINDS)}; got {kind!r}")
        command = raw.get("command")
        if isinstance(command, str):
            if not command.strip():
                raise ValueError(f"hooks[{idx}].command must be non-empty")
            command = command.strip()
        elif isinstance(command, list) and command and all(isinstance(c, str) and c for c in command):
            command = list(command)
        else:
            raise ValueError(f"hooks[{idx}].command must be a non-empty string or argv list")
        optional = raw.get("optional", False)
        if not isinstance(optional, bool):
            raise ValueError(f"hooks[{idx}].optional must be a boolean")
        hook: dict[str, Any] = {"name": name, "kind": kind, "command": command, "optional": optional}
        if raw.get("timeout") is not None:
            timeout = raw["timeout"]
            if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
                raise ValueError(f"hooks[{idx}].timeout must be a positive integer (seconds)")
            hook["timeout"] = timeout
        if raw.get("description"):
            hook["description"] = str(raw["description"])
        hook.update(_validate_hook_metadata(raw, kind, f"hooks[{idx}] ({name})"))
        seen.add(name)
        out.append(hook)
    return out


# Optional per-kind hook metadata: formal invariants (`covers` / `sync_commit`) and frozen visual baselines.
INVARIANT_META_KEYS: tuple[str, ...] = ("covers", "sync_commit")
VISUAL_META_KEYS: tuple[str, ...] = ("baseline_paths", "frozen_baseline")
HOOK_META_KEYS: tuple[str, ...] = (*INVARIANT_META_KEYS, *VISUAL_META_KEYS)


def _glob_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v.strip() for v in value):
        raise ValueError(f"{label} must be a non-empty list of non-empty glob strings; got {value!r}")
    return [v.strip() for v in value]


def _validate_hook_metadata(raw: dict[str, Any], kind: str, label: str) -> dict[str, Any]:
    """Normalize the optional kind-specific metadata of one hook; ``ValueError`` on misuse."""
    present = {k: raw[k] for k in HOOK_META_KEYS if raw.get(k) is not None}
    for key in present:
        owner = "invariant" if key in INVARIANT_META_KEYS else "visual_diff"
        if kind != owner:
            raise ValueError(f"{label}.{key} is only allowed on `{owner}` hooks; got kind {kind!r}")
    meta: dict[str, Any] = {}
    if "covers" in present:
        meta["covers"] = _glob_list(present["covers"], f"{label}.covers")
    if "sync_commit" in present:
        if "covers" not in meta:
            raise ValueError(f"{label}.sync_commit requires a non-empty `covers` glob list")
        value = present["sync_commit"]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label}.sync_commit must be a non-empty git commit SHA or ref; got {value!r}")
        ref = value.strip()
        if ref.startswith("-") or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in ref):
            raise ValueError(
                f"{label}.sync_commit must not start with '-' or contain whitespace/control characters; got {value!r}"
            )
        meta["sync_commit"] = ref
    if "baseline_paths" in present:
        meta["baseline_paths"] = _glob_list(present["baseline_paths"], f"{label}.baseline_paths")
    if "frozen_baseline" in present:
        if not isinstance(present["frozen_baseline"], bool):
            raise ValueError(f"{label}.frozen_baseline must be a boolean")
        meta["frozen_baseline"] = present["frozen_baseline"]
    elif "baseline_paths" in meta:
        meta["frozen_baseline"] = True
    return meta


def _hook_metadata_md(hook: dict[str, Any]) -> str:
    """Trailing ` [covers: `a/**`; sync_commit: `abc`]` rendered after a hook's backticked command."""
    parts: list[str] = []
    for key in HOOK_META_KEYS:
        value = hook.get(key)
        if value is None:
            continue
        if isinstance(value, list):
            parts.append(f"{key}: " + ", ".join(f"`{v}`" for v in value))
        else:
            parts.append(f"{key}: `{str(value).lower() if isinstance(value, bool) else value}`")
    return f" [{'; '.join(parts)}]" if parts else ""


_HOOK_META_RE = re.compile(r"\b(covers|sync_commit|baseline_paths|frozen_baseline):\s*((?:`[^`\n]*`\s*,?\s*)+)")


def _parse_hook_metadata_md(rest: str) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    for key, raw in _HOOK_META_RE.findall(rest):
        values = re.findall(r"`([^`\n]*)`", raw)
        if key in ("covers", "baseline_paths"):
            meta[key] = values
        elif key == "frozen_baseline":
            meta[key] = {"true": True, "false": False}.get(values[0].strip().lower(), values[0]) if values else None
        elif values:
            meta[key] = values[0]
    return meta


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def _build_feature_map(profile: dict[str, Any]) -> list[dict[str, Any]]:
    test_cmds = list(profile["commands"]["test"])
    groups: dict[str, list[str]] = {}
    for rel in profile["source_files"]:
        top = rel.split("/", 1)[0] if "/" in rel else ""
        groups.setdefault(top, []).append(rel)
    features: list[dict[str, Any]] = []
    # Root files claim "root" first; normalized directory-slug collisions get deterministic -2, -3 suffixes.
    taken: set[str] = set()
    for top in sorted(groups):
        members = groups[top]
        if top:
            fid = _unique_id(_slug_id(top), taken)
            paths = [f"{top}/**"]
        else:
            fid = _unique_id("root", taken)
            exts = sorted({Path(m).suffix for m in members})
            paths = [f"*{ext}" for ext in exts]
        feat_routes = [r for r, src in profile["route_sources"].items() if src in members]
        feat_cli = [c for c, src in profile["cli_sources"].items() if src in members]
        if top in _TEST_DIRS:
            surface = "Tests"
        elif feat_routes:
            surface = "HTTP API"
        elif feat_cli:
            surface = "CLI"
        else:
            surface = "Module"
        scenarios = [f"{r} responds as documented" for r in feat_routes[:10]]
        scenarios += [f"`{c}` subcommand runs and exits 0 with --help" for c in feat_cli[:10]]
        features.append({
            "id": fid,
            "surface": surface,
            "paths": paths,
            "commands": test_cmds[:3],
            "scenarios": scenarios,
        })
    return features


def _phases_for(profile: dict[str, Any]) -> dict[str, list[str]]:
    cmds = profile["commands"]
    doctor = ["git --version"]
    tool_checks = {"python": "python3 --version", "node": "node --version", "rust": "cargo --version", "go": "go version"}
    doctor += [cmd for lang, cmd in tool_checks.items() if lang in profile["stack"]]
    drive = [*cmds["test"], *cmds["lint"]]
    drive += [
        c for c in profile["ci_commands"] if c not in drive and _is_ci_verification_command(c)
    ][:10]
    return {
        "launch": [*cmds["setup"], *cmds["build"], *cmds["start"]],
        "doctor": doctor,
        "drive": drive,
        "evidence": [
            "Record every command with its exit code and the last lines of stdout/stderr.",
            "Capture HTTP status + response bodies for each probed route and screenshots for UI surfaces.",
        ],
        "cleanup": [
            "Stop any background servers or processes started during Launch/Drive.",
            "Remove temporary directories, ports, and test data created during verification.",
        ],
    }


def _fmt_cmd(command: Any) -> str:
    return " ".join(command) if isinstance(command, list) else str(command)


def render_verify_md(manifest: dict[str, Any]) -> str:
    """Render the canonical `.factonio/verify.md` playbook from a harness manifest."""
    phases = manifest.get("phases") or {}
    lines: list[str] = [
        f"# Verification Harness: {manifest.get('repo') or 'repository'}",
        "",
        "Black-box playbook for Factonio `[verify:blackbox]` cards and humans. Run every stage in order",
        "(Launch -> Doctor -> Drive -> Evidence -> Cleanup) against a fresh checkout of the change under test.",
        "This file describes how to run and observe the software; it is not implementation source.",
        "",
        f"- Schema: `{HARNESS_DIR_NAME}/{HARNESS_JSON_NAME}` (schema_version {HARNESS_SCHEMA_VERSION})",
        f"- Detected stack: {', '.join(manifest.get('stack') or []) or 'unknown'}",
        f"- Generated at: {manifest.get('generated_at') or '-'} from `{manifest.get('base_commit') or 'working tree'}`",
        "",
    ]
    descriptions = {
        "Launch": "Install, build, and start the artifact or CLI in a clean checkout.",
        "Doctor": "Prove the environment is ready before driving it (each command must exit 0).",
        "Drive": "Exercise the black-box surfaces: commands, HTTP probes, CLI or UI interactions.",
        "Evidence": "Capture what proves each behavior.",
        "Cleanup": "Tear down everything Launch and Drive started.",
    }
    for section in REQUIRED_SECTIONS:
        key = section.lower()
        items = [str(x) for x in phases.get(key) or []]
        lines += [f"## {section}", "", descriptions[section], ""]
        if key in {"evidence", "cleanup"}:
            lines += [f"- {item}" for item in items] or ["- (none declared)"]
        elif items:
            lines += ["```bash", *items, "```"]
        else:
            lines += ["_No commands detected; document them here._"]
        lines.append("")
    surfaces = manifest.get("discovered_surfaces") or {}
    if surfaces.get("routes") or surfaces.get("cli_commands"):
        lines += ["### Discovered surfaces", ""]
        if surfaces.get("routes"):
            lines.append("- HTTP routes: " + ", ".join(f"`{r}`" for r in surfaces["routes"]))
        if surfaces.get("cli_commands"):
            lines.append("- CLI commands: " + ", ".join(f"`{c}`" for c in surfaces["cli_commands"]))
        lines.append("")
    lines += ["## Feature Map", "", "| Feature | Surface | Paths | Verify with |", "|---|---|---|---|"]
    for feat in manifest.get("feature_map") or []:
        paths = ", ".join(f"`{p}`" for p in feat.get("paths") or [])
        verify = "; ".join([*(f"`{c}`" for c in feat.get("commands") or []), *(feat.get("scenarios") or [])][:4]) or "-"
        lines.append(f"| `{feat.get('id')}` | {feat.get('surface') or '-'} | {paths} | {verify} |")
    lines += ["", "## Hooks", "", "Repo-defined checks (`visual_diff`, `invariant`, `custom`). Required hook failures reject; optional ones are disclosed limits.", ""]
    hooks = manifest.get("hooks") or []
    for hook in hooks:
        req = "optional" if hook.get("optional") else "required"
        lines.append(
            f"- **{hook['name']}** (`{hook['kind']}`, {req}): `{_fmt_cmd(hook['command'])}`{_hook_metadata_md(hook)}"
        )
    if not hooks:
        lines.append("_No hooks declared._")
    lines.append("")
    return "\n".join(lines)


def _atomic_write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    data = content.encode("utf-8") if isinstance(content, str) else content
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _unproven() -> dict[str, Any]:
    return {"ran": False, "ok": None, "doctor": None, "hooks": None, "evidence_survived": None, "errors": []}


def _prove_evidence_survives() -> dict[str, Any]:
    """Write an evidence artifact, delete the scratch workdir (simulated Cleanup), and re-read the artifact."""
    root = Path(tempfile.mkdtemp(prefix="factonio-harness-proof-"))
    survived, error = False, None
    try:
        work, evidence = root / "work", root / "evidence"
        work.mkdir()
        evidence.mkdir()
        payload = {"probe": "evidence-survival", "nonce": secrets.token_hex(8), "written_at": _now_iso()}
        (work / "scratch.txt").write_text("scratch\n", encoding="utf-8")
        artifact = evidence / "proof-evidence.json"
        artifact.write_text(json.dumps(payload), encoding="utf-8")
        shutil.rmtree(work)
        if work.exists():
            error = "Cleanup left the scratch workdir behind"
        elif not artifact.is_file():
            error = "Evidence artifact did not survive workdir cleanup"
        elif json.loads(artifact.read_text(encoding="utf-8")) != payload:
            error = "Evidence artifact was altered by workdir cleanup"
        else:
            survived = True
    except (OSError, ValueError) as exc:
        error = f"Evidence survival probe failed: {exc}"
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return {"ok": survived, "error": error, "temp_root_removed": not root.exists()}


def _run_harness_proof(repo_path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Run Doctor, every declared hook, and the evidence-survival probe; ``ok`` only when all pass."""
    errors: list[str] = []
    doctor = _run_doctor(repo_path, list(manifest["phases"].get("doctor") or []))
    for r in doctor["results"]:
        if not r["ok"]:
            errors.append(f"Doctor command failed: `{_fmt_cmd(r['command'])}` (exit {r['exit_code']})")
    hooks = run_harness_hooks(repo_path, hooks=manifest["hooks"])
    for name in hooks["failed_required"]:
        errors.append(f"Required hook {name!r} failed")
    evidence = _prove_evidence_survives()
    if not evidence["ok"]:
        errors.append(evidence["error"] or "Evidence artifact did not survive workdir cleanup")
    return {
        "ran": True,
        "ok": not errors,
        "doctor": doctor,
        "hooks": hooks,
        "evidence_survived": bool(evidence["ok"]),
        "errors": errors,
    }


def _restore_harness_files(hdir: Path, snapshot: dict[Path, Optional[bytes]], dir_existed: bool) -> None:
    for path, prior in snapshot.items():
        if prior is None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        else:
            _atomic_write(path, prior)
    if not dir_existed:
        try:
            hdir.rmdir()
        except OSError:
            pass  # not empty: something other than our two files lives there now


def bootstrap_verify_harness(
    repo_spec: str,
    *,
    force: bool = False,
    hooks: Optional[list[dict[str, Any]]] = None,
    dry_run: bool = False,
    tenant: Optional[str] = None,
    card_id: Optional[str] = None,
    prove: bool = False,
) -> dict[str, Any]:
    """Generate `.factonio/verify.md` + `.factonio/harness.json` (refuses overwrite unless ``force``).

    With ``prove`` the generated harness must pass Doctor, every required hook, and an
    evidence-survival probe; otherwise it fails closed and restores the prior `.factonio/` state.
    """
    target = _resolve_repo(repo_spec)
    repo_path: Path = target["path"]
    hdir, verify_md, harness_json = _harness_paths(repo_path)
    hook_list = validate_hooks(hooks)

    existing = [str(p) for p in (verify_md, harness_json) if p.exists()]
    base = {
        "repo": target["repo"],
        "repo_path": str(repo_path),
        "harness_dir": str(hdir),
        "verify_md": str(verify_md),
        "harness_json": str(harness_json),
        "dry_run": bool(dry_run),
    }
    if existing and not force and not dry_run:
        return {
            **base,
            "ok": False,
            "written": False,
            "refused": True,
            "existing": existing,
            "proof": _unproven(),
            "error": f"Harness already exists ({', '.join(existing)}); pass force=True (--force) to overwrite.",
        }

    preserved_hooks = False
    if hooks is None and existing:
        prior = _load_json(harness_json)
        try:
            hook_list = validate_hooks(prior.get("hooks"))
            preserved_hooks = bool(hook_list)
        except ValueError:
            hook_list = []

    profile = detect_repo_profile(repo_path)
    manifest: dict[str, Any] = {
        "schema_version": HARNESS_SCHEMA_VERSION,
        "repo": target["repo"],
        "generated_at": _now_iso(),
        "base_commit": _head_commit(repo_path) if _is_git_repo(repo_path) else None,
        "stack": profile["stack"],
        "phases": _phases_for(profile),
        "feature_map": _build_feature_map(profile),
        "hooks": hook_list,
        "discovered_surfaces": {
            "routes": profile["routes"],
            "cli_commands": profile["cli_commands"],
            "entrypoints": profile["entrypoints"],
        },
    }
    md_text = render_verify_md(manifest)
    json_text = json.dumps(manifest, indent=2) + "\n"
    result = {
        **base,
        "ok": True,
        "refused": False,
        "existing": existing,
        "would_overwrite": bool(existing),
        "overwritten": bool(existing) and not dry_run,
        "preserved_hooks": preserved_hooks,
        "manifest": manifest,
        "profile": {k: v for k, v in profile.items() if k != "source_files"},
        "proof": _unproven(),
    }
    if dry_run:
        dry = {**result, "written": False, "verify_md_content": md_text, "harness_json_content": json_text}
        if prove:
            proof = _run_harness_proof(repo_path, manifest)
            dry["proof"] = proof
            if not proof["ok"]:
                dry.update(ok=False, error="Harness proof failed: " + "; ".join(proof["errors"]))
        return dry

    snapshot = {p: (p.read_bytes() if p.is_file() else None) for p in (harness_json, verify_md)} if prove else {}
    dir_existed = hdir.is_dir()
    from .trail import record_transition

    try:
        _atomic_write(harness_json, json_text)
        _atomic_write(verify_md, md_text)
        proof = _run_harness_proof(repo_path, manifest) if prove else None
    except BaseException:
        if prove:
            _restore_harness_files(hdir, snapshot, dir_existed)
        raise
    if proof is not None:
        result["proof"] = proof
    if proof is not None and not proof["ok"]:
        _restore_harness_files(hdir, snapshot, dir_existed)
        row = record_transition(
            "bootstrap_verify",
            tenant=tenant or "",
            repo_slug=target["slug"] or target["repo"],
            phase="harness",
            card_id=card_id or "",
            why=f"Harness proof failed; restored prior {HARNESS_DIR_NAME}/ state",
            evidence={
                "repo_path": str(repo_path),
                "proof": {
                    "errors": proof["errors"],
                    "doctor_ok": proof["doctor"]["ok"],
                    "failed_required_hooks": proof["hooks"]["failed_required"],
                    "evidence_survived": proof["evidence_survived"],
                },
                "base_commit": manifest["base_commit"],
                "force": bool(force),
            },
            result="proof_failed",
        )
        return {
            **result,
            "ok": False,
            "written": False,
            "overwritten": False,
            "rolled_back": True,
            "error": "Harness proof failed: " + "; ".join(proof["errors"]),
            "trail_decision_id": (row or {}).get("id"),
        }

    evidence: dict[str, Any] = {
        "repo_path": str(repo_path),
        "files": [str(verify_md), str(harness_json)],
        "base_commit": manifest["base_commit"],
        "stack": manifest["stack"],
        "features": len(manifest["feature_map"]),
        "hooks": [h["name"] for h in hook_list],
        "routes": len(profile["routes"]),
        "cli_commands": len(profile["cli_commands"]),
        "force": bool(force),
    }
    if proof is not None:
        evidence["proof"] = {"ok": True, "doctor": proof["doctor"]["ran"], "hooks": proof["hooks"]["ran"], "evidence_survived": True}
    row = record_transition(
        "bootstrap_verify",
        tenant=tenant or "",
        repo_slug=target["slug"] or target["repo"],
        phase="harness",
        card_id=card_id or "",
        why=f"Bootstrapped {HARNESS_DIR_NAME}/ verification harness"
        + (" (forced overwrite)" if existing else "")
        + (" and proved it" if proof is not None else ""),
        evidence=evidence,
        result="overwritten" if existing else "written",
    )
    return {**result, "written": True, "trail_decision_id": (row or {}).get("id")}


# ---------------------------------------------------------------------------
# Discovery / parsing
# ---------------------------------------------------------------------------


def _split_sections(md_text: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    matches = list(_H2_RE.finditer(md_text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md_text)
        sections.setdefault(m.group(1).strip(), md_text[m.end():end])
    return sections


def _section_commands(body: str) -> list[str]:
    cmds: list[str] = []
    for block in _FENCE_RE.findall(body):
        for line in block.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                cmds.append(line)
    return cmds


def _parse_md_hooks(body: str) -> list[dict[str, Any]]:
    hooks: list[dict[str, Any]] = []
    for m in _HOOK_LINE_RE.finditer(body):
        eol = body.find("\n", m.end())
        hooks.append({
            "name": m.group("name").strip(),
            "kind": m.group("kind"),
            "command": m.group("cmd").strip(),
            "optional": m.group("opt") == "optional",
            **_parse_hook_metadata_md(body[m.end(): eol if eol >= 0 else len(body)]),
        })
    return hooks


def _effective_hooks_and_phases(
    manifest: Optional[dict[str, Any]], md_sections: Optional[dict[str, str]]
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """Per-field fallback shared by discovery and the gate-loosening baseline: each of `hooks` / `phases`
    comes from harness.json when that key is set there, else from verify.md (``md_sections`` None = absent).
    Malformed manifest fields are reported by check_harness; only well-typed values are exposed here."""
    hooks: list[dict[str, Any]] = []
    if manifest is not None and manifest.get("hooks") is not None:
        raw_hooks = manifest["hooks"]
        hooks = [h for h in raw_hooks if isinstance(h, dict)] if isinstance(raw_hooks, list) else []
    elif md_sections is not None and "Hooks" in md_sections:
        hooks = _parse_md_hooks(md_sections["Hooks"])
    phases: dict[str, list[str]] = {}
    if manifest is not None and isinstance(manifest.get("phases"), dict):
        raw_phases = manifest["phases"]
        phases = {k: list(raw_phases[k]) if _is_str_list(raw_phases.get(k)) else [] for k in PHASE_KEYS}
    elif manifest is not None and manifest.get("phases") is not None:
        phases = {k: [] for k in PHASE_KEYS}
    elif md_sections is not None:
        phases = {s.lower(): _section_commands(md_sections.get(s, "")) for s in REQUIRED_SECTIONS}
    return hooks, phases


def discover_harness(repo_path: str | Path) -> dict[str, Any]:
    """Read-only discovery of `<repo>/.factonio/` (full harness or a hand-authored `verify.md` only)."""
    root = Path(repo_path).expanduser()
    hdir, verify_md, harness_json = _harness_paths(root)
    has_md = verify_md.is_file()
    has_json = harness_json.is_file()
    md_text = _read_text(verify_md) if has_md else ""
    sections_map = _split_sections(md_text) if has_md else {}
    sections = list(sections_map)
    manifest: Optional[dict[str, Any]] = None
    manifest_error: Optional[str] = None
    if has_json:
        try:
            loaded = json.loads(_read_text(harness_json))
            if isinstance(loaded, dict):
                manifest = loaded
            else:
                manifest_error = "harness.json must contain a JSON object"
        except ValueError as exc:
            manifest_error = f"harness.json is not valid JSON: {exc}"
    hooks, phases = _effective_hooks_and_phases(manifest, sections_map if has_md else None)
    return {
        "present": has_md,
        "mode": "full" if (has_md and has_json) else ("verify_md_only" if has_md else ("json_only" if has_json else "absent")),
        "harness_dir": str(hdir),
        "verify_md": str(verify_md) if has_md else None,
        "harness_json": str(harness_json) if has_json else None,
        "sections": sections,
        "missing_sections": [s for s in REQUIRED_SECTIONS if s not in sections_map] if has_md else list(REQUIRED_SECTIONS),
        "manifest": manifest,
        "manifest_error": manifest_error,
        "phases": phases,
        "hooks": hooks,
    }


def show_harness(repo_spec: str) -> dict[str, Any]:
    """Return discovered harness metadata, parsed `harness.json`, and `verify.md` content."""
    target = _resolve_repo(repo_spec)
    info = discover_harness(target["path"])
    content = _read_text(Path(info["verify_md"])) if info["verify_md"] else None
    return {
        "ok": bool(info["present"]),
        "repo": target["repo"],
        "repo_path": str(target["path"]),
        **info,
        "verify_md_content": content,
        **({} if info["present"] else {"error": f"No {HARNESS_DIR_NAME}/{VERIFY_MD_NAME} found; run `hermes factonio harness init`."}),
    }


# ---------------------------------------------------------------------------
# Command and hook execution
# ---------------------------------------------------------------------------


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """SIGKILL the command's whole process group (POSIX) so shell descendants cannot outlive a timeout."""
    if os.name != "posix":
        proc.kill()
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        proc.poll()  # reap the leader so its zombie does not keep the group alive
        try:
            os.killpg(proc.pid, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.02)


def _run_command(command: Any, cwd: Path, timeout: int) -> dict[str, Any]:
    started = time.monotonic()
    shell = isinstance(command, str)
    # A dedicated session/process group lets a timeout kill every descendant, not just `/bin/sh`.
    isolation: dict[str, Any] = (
        {"start_new_session": True} if os.name == "posix"
        else {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    )
    code: Optional[int] = None
    out, err, timed_out = "", "", False
    try:
        proc = subprocess.Popen(
            command, cwd=str(cwd), shell=shell, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, errors="replace", **isolation,
        )
    except OSError as exc:
        code, err = 127, str(exc)
    else:
        try:
            try:
                out, err = proc.communicate(timeout=timeout)
                code = proc.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_process_tree(proc)
                try:
                    out, err = proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:  # a descendant escaped the group and holds the pipes
                    out, err = "", ""
                err = ((err or "") + f"\nTimed out after {timeout}s").strip()
        finally:
            if proc.poll() is None:
                _kill_process_tree(proc)
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None:
                    pipe.close()
            proc.wait()
    return {
        "command": command,
        "exit_code": code,
        "ok": code == 0 and not timed_out,
        "timed_out": timed_out,
        "duration_s": round(time.monotonic() - started, 3),
        "stdout": _tail(out),
        "stderr": _tail(err),
    }


def run_harness_hooks(
    repo_path: str | Path,
    *,
    hooks: Optional[list[dict[str, Any]]] = None,
    kinds: Optional[list[str]] = None,
    timeout: int = DEFAULT_HOOK_TIMEOUT,
) -> dict[str, Any]:
    """Run target-repo hooks in ``repo_path``; ``ok`` is False only when a required hook fails."""
    root = Path(repo_path).expanduser()
    if kinds is not None:
        bad = [k for k in kinds if k not in HOOK_KINDS]
        if bad:
            raise ValueError(f"Unknown hook kind(s): {', '.join(bad)}. Allowed: {', '.join(HOOK_KINDS)}")
    hook_list = validate_hooks(hooks if hooks is not None else discover_harness(root)["hooks"])
    if kinds is not None:
        hook_list = [h for h in hook_list if h["kind"] in kinds]
    results: list[dict[str, Any]] = []
    warnings: list[str] = []
    failed_required: list[str] = []
    for hook in hook_list:
        res = _run_command(hook["command"], root, int(hook.get("timeout") or timeout))
        entry = {"name": hook["name"], "kind": hook["kind"], "optional": hook["optional"], **res}
        results.append(entry)
        if not res["ok"]:
            why = "timed out" if res["timed_out"] else f"exit {res['exit_code']}"
            if hook["optional"]:
                warnings.append(f"Optional hook {hook['name']!r} ({hook['kind']}) failed: {why}")
            else:
                failed_required.append(hook["name"])
    return {
        "ok": not failed_required,
        "ran": len(results),
        "passed": sum(1 for r in results if r["ok"]),
        "failed_required": failed_required,
        "warnings": warnings,
        "results": results,
    }


def _run_doctor(root: Path, commands: list[str], timeout: int = DEFAULT_HOOK_TIMEOUT) -> dict[str, Any]:
    results = [_run_command(cmd, root, timeout) for cmd in commands]
    return {"ok": all(r["ok"] for r in results), "ran": len(results), "results": results}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_manifest(manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    version = manifest.get("schema_version")
    if version != HARNESS_SCHEMA_VERSION:
        errors.append(f"harness.json schema_version must be {HARNESS_SCHEMA_VERSION}; got {version!r}")
    phases = manifest.get("phases")
    if not isinstance(phases, dict):
        errors.append("harness.json `phases` must be an object with launch/doctor/drive/evidence/cleanup")
    else:
        for key in PHASE_KEYS:
            if not _is_str_list(phases.get(key)):
                errors.append(f"harness.json `phases.{key}` must be a list of strings; got {phases.get(key)!r}")
    fmap = manifest.get("feature_map")
    if not isinstance(fmap, list):
        errors.append(f"harness.json `feature_map` must be a list; got {type(fmap).__name__}")
    else:
        ids: set[str] = set()
        for idx, feat in enumerate(fmap):
            fid = feat.get("id") if isinstance(feat, dict) else None
            if not isinstance(fid, str) or not fid.strip():
                errors.append(f"feature_map[{idx}] must be an object with a non-empty string `id`; got {fid!r}")
                continue
            if fid in ids:
                errors.append(f"feature_map has duplicate id {fid!r}")
            ids.add(fid)
            if not _is_str_list(feat.get("paths"), non_empty_items=True) or not feat.get("paths"):
                errors.append(f"feature_map[{idx}] ({fid}) `paths` must be a non-empty list of glob strings")
            for key in ("commands", "scenarios"):
                if key in feat and not _is_str_list(feat[key]):
                    errors.append(f"feature_map[{idx}] ({fid}) `{key}` must be a list of strings")
            if "surface" in feat and not isinstance(feat["surface"], str):
                errors.append(f"feature_map[{idx}] ({fid}) `surface` must be a string")
    surfaces = manifest.get("discovered_surfaces")
    if surfaces is not None:
        if not isinstance(surfaces, dict):
            errors.append("harness.json `discovered_surfaces` must be an object")
        else:
            for key in ("routes", "cli_commands", "entrypoints"):
                if key in surfaces and not _is_str_list(surfaces[key]):
                    errors.append(f"harness.json `discovered_surfaces.{key}` must be a list of strings")
    base_commit = manifest.get("base_commit")
    if base_commit is not None and not isinstance(base_commit, str):
        errors.append("harness.json `base_commit` must be a string or null")
    if "stack" in manifest and not _is_str_list(manifest["stack"]):
        errors.append("harness.json `stack` must be a list of strings")
    try:
        validate_hooks(manifest.get("hooks", []))
    except ValueError as exc:
        errors.append(f"harness.json hooks invalid: {exc}")
    return errors


def _dead_globs(manifest: Optional[dict[str, Any]], files: list[str]) -> list[dict[str, str]]:
    dead: list[dict[str, str]] = []
    if not manifest or not isinstance(manifest.get("feature_map"), list):
        return dead
    for feat in manifest["feature_map"]:
        if not isinstance(feat, dict):
            continue
        for pattern in _str_list(feat.get("paths")):
            if pattern.strip() and not any(glob_matches(pattern, f) for f in files):
                dead.append({"feature": str(feat.get("id")), "glob": pattern})
    return dead


def check_harness(
    repo_spec: str, *, run_doctor: bool = False, run_hooks: bool = False, base_ref: Optional[str] = None
) -> dict[str, Any]:
    """Validate presence, required sections, manifest schema, live globs, and gate loosening vs ``base_ref``
    (default ``HEAD``); optionally run Doctor/hooks."""
    target = _resolve_repo(repo_spec)
    root: Path = target["path"]
    gate = detect_harness_gate_loosening(root, base_ref)
    info = discover_harness(root)
    errors: list[str] = []
    warnings: list[str] = []
    dead: list[dict[str, str]] = []
    if not info["present"]:
        errors.append(f"Missing {HARNESS_DIR_NAME}/{VERIFY_MD_NAME}; run `hermes factonio harness init <repo>`.")
    else:
        for section in info["missing_sections"]:
            errors.append(f"verify.md is missing required `## {section}` section")
        for section in EXTRA_SECTIONS:
            if section not in info["sections"]:
                warnings.append(f"verify.md has no `## {section}` section")
    manifest_errors: list[str] = []
    if info["manifest_error"]:
        errors.append(info["manifest_error"])
    elif info["manifest"] is not None:
        manifest_errors = _validate_manifest(info["manifest"])
        errors.extend(manifest_errors)
        files, _ = _list_repo_files(root)
        dead = _dead_globs(info["manifest"], files)
        for d in dead:
            errors.append(f"feature_map `{d['feature']}` glob `{d['glob']}` matches no files")
    elif info["present"]:
        warnings.append(f"No {HARNESS_DIR_NAME}/{HARNESS_JSON_NAME}; using hand-authored verify.md only")
    md_hook_errors = _verify_md_hook_errors(info)
    errors.extend(md_hook_errors)
    formal = _check_formal_invariants(root, info)
    errors.extend(formal["errors"])

    doctor = None
    hooks_res = None
    if manifest_errors and (run_doctor or run_hooks):
        warnings.append("Skipped Doctor commands and hooks: harness.json is malformed")
        run_doctor = run_hooks = False
    # Unresolved sync_commits and malformed verify.md hooks are harness gaps too, but they must not mask a real
    # hook failure.
    manifest_errors = manifest_errors + md_hook_errors + formal["manifest_errors"]
    pre_sha, pre_frozen = (_pre_execution_frozen_contract(root, gate, info["hooks"]) if run_doctor or run_hooks
                           else (None, {}))
    if run_doctor:
        cmds = info["phases"].get("doctor") or []
        doctor = _run_doctor(root, cmds)
        if not cmds:
            warnings.append("Doctor section declares no commands")
        for r in doctor["results"]:
            if not r["ok"]:
                errors.append(f"Doctor command failed: `{_fmt_cmd(r['command'])}` (exit {r['exit_code']})")
    if run_hooks:
        try:
            hooks_res = run_harness_hooks(root, hooks=info["hooks"])
            warnings.extend(hooks_res["warnings"])
            for name in hooks_res["failed_required"]:
                errors.append(f"Required hook {name!r} failed")
        except ValueError as exc:
            if not md_hook_errors:
                errors.append(str(exc))
    if run_doctor or run_hooks:
        # A Doctor command or hook may regenerate a frozen baseline while it runs: re-check after execution,
        # both against the live harness and against the contract fixed before anything ran (execution may also
        # unfreeze, remove or rewrite the hook, or move HEAD).
        gate = _merge_gate(gate, detect_harness_gate_loosening(root, base_ref))
        if pre_frozen and pre_sha:
            gate = _merge_gate(gate, _frozen_contract_gate(root, pre_sha, pre_frozen, base_ref))
    if gate["loosened"]:
        errors.extend(gate["findings"])

    valid = bool(info["present"]) and not errors
    return {
        "ok": valid,
        "valid": valid,
        "repo": target["repo"],
        "repo_path": str(root),
        "present": info["present"],
        "mode": info["mode"],
        "verify_md": info["verify_md"],
        "harness_json": info["harness_json"],
        "schema_version": (info["manifest"] or {}).get("schema_version"),
        "sections": info["sections"],
        "missing_sections": info["missing_sections"],
        "dead_globs": dead,
        "manifest_errors": ([info["manifest_error"]] if info["manifest_error"] else []) + manifest_errors,
        "hooks": info["hooks"],
        "errors": errors,
        "warnings": warnings,
        "doctor": doctor,
        "hook_results": hooks_res,
        "gate_loosening": gate,
        "formal_invariants": formal,
        "visual_baseline_changes": list(gate.get("visual_baseline_changes") or []),
    }


def _verify_md_hook_errors(info: dict[str, Any]) -> list[str]:
    """Schema errors of hooks sourced from verify.md (no harness.json, or one without `hooks`), which
    `_validate_manifest` never sees."""
    manifest = info.get("manifest")
    if not info.get("present") or not info.get("hooks") or (manifest is not None and manifest.get("hooks") is not None):
        return []
    try:
        validate_hooks(info["hooks"])
    except ValueError as exc:
        return [f"{VERIFY_MD_NAME} hooks invalid: {exc}"]
    return []


def _merge_gate(gate: dict[str, Any], later: dict[str, Any]) -> dict[str, Any]:
    """Union of two gate-loosening results for the same base (e.g. before and after hooks ran)."""
    if not later.get("checked"):
        return gate
    merged = dict(gate) if gate.get("checked") else dict(later)
    for key in ("removed_required_hooks", "downgraded_hooks", "removed_doctor_commands", "loosened_visual_hooks",
                "findings"):
        merged[key] = list(dict.fromkeys([*(gate.get(key) or []), *(later.get(key) or [])]))
    merged["visual_baseline_changes"] = _merge_baseline_changes(
        list(gate.get("visual_baseline_changes") or []), list(later.get("visual_baseline_changes") or [])
    )
    merged["loosened"] = bool(merged["findings"])
    return merged


def _check_formal_invariants(root: Path, info: dict[str, Any]) -> dict[str, Any]:
    """`_audit_formal_invariants` for `check_harness`: stale invariants since the harness base (warnings-free)."""
    if not info["present"] or not _formal_invariant_hooks(info["hooks"]):
        return _audit_formal_invariants(root, [], files=[], harness_base=None, harness_base_source=None, is_git=False)
    is_git = _is_git_repo(root)
    base_sha, base_source = _audit_base(root, None, info["manifest"]) if is_git else (None, None)
    return _audit_formal_invariants(
        root, info["hooks"], files=_invariant_files(root), harness_base=base_sha, harness_base_source=base_source,
        is_git=is_git,
    )


# ---------------------------------------------------------------------------
# Formal invariants (`covers` / `sync_commit`) and frozen visual baselines (read-only)
# ---------------------------------------------------------------------------


def _safe_ref(value: Any) -> Optional[str]:
    try:
        return _validate_ref(value)
    except ValueError:
        return None


def _formal_invariant_hooks(hooks: Iterable[Any]) -> list[dict[str, Any]]:
    """Well-typed `invariant` hooks declaring a non-empty `covers` glob list (malformed ones are schema errors)."""
    return [
        h for h in hooks
        if isinstance(h, dict) and h.get("kind") == "invariant"
        and _is_str_list(h.get("covers"), non_empty_items=True) and h.get("covers")
    ]


def _audit_formal_invariants(
    root: Path,
    hooks: Iterable[Any],
    *,
    files: list[str],
    harness_base: Optional[str],
    harness_base_source: Optional[str],
    is_git: bool,
) -> dict[str, Any]:
    """Read-only formal-invariant drift: dead `covers` globs, unresolved `sync_commit`s, and invariants whose
    covered files changed since `sync_commit` (or the harness base when omitted). Every git probe is `_git_ro`."""
    result: dict[str, Any] = {
        "checked": False,
        "invariants": [],
        "dead_cover_globs": [],
        "unresolved_sync_commits": [],
        "stale_invariants": [],
        "errors": [],
        "manifest_errors": [],
    }
    invariants = _formal_invariant_hooks(hooks)
    if not invariants:
        return result
    result["checked"] = True
    changed_cache: dict[str, list[str]] = {}
    for hook in invariants:
        name = str(hook.get("name") or "").strip()
        covers = [c.strip() for c in hook["covers"]]
        raw_sync = hook.get("sync_commit")
        declared = raw_sync.strip() if isinstance(raw_sync, str) and raw_sync.strip() else None
        entry: dict[str, Any] = {
            "name": name, "covers": covers, "sync_commit": declared, "sync_source": None, "base_commit": None,
            "changed_files": [], "optional": bool(hook.get("optional", False)), "status": "unchecked",
        }
        result["invariants"].append(entry)
        for glob in covers:
            if not any(glob_matches(glob, f) for f in files):
                result["dead_cover_globs"].append({"hook": name, "glob": glob})
                result["errors"].append(f"Formal invariant {name!r} covers glob `{glob}` matches no files")
        if declared is not None:
            ref = _safe_ref(declared)
            sha = _resolve_commit(root, ref) if is_git and ref else None
            if not sha:
                entry["status"] = "unresolved"
                result["unresolved_sync_commits"].append({"name": name, "sync_commit": declared})
                msg = f"Formal invariant {name!r} sync_commit `{declared}` does not resolve to a commit"
                result["errors"].append(msg)
                result["manifest_errors"].append(msg)
                continue
            entry.update(sync_source="sync_commit", base_commit=sha)
        elif is_git and harness_base:
            sha = harness_base
            entry.update(sync_commit=harness_base, sync_source=harness_base_source, base_commit=harness_base)
        else:
            continue
        if sha not in changed_cache:
            changed_cache[sha] = _changed_files(root, sha, include_harness_dir=True)
        hits = [p for p in changed_cache[sha] if _any_match(covers, p)]
        entry["changed_files"] = hits
        entry["status"] = "stale" if hits else "in_sync"
        if hits:
            result["stale_invariants"].append(
                {k: entry[k] for k in ("name", "sync_commit", "sync_source", "covers", "changed_files", "optional")}
            )
    return result


def _frozen_baseline_globs(hook: dict[str, Any]) -> list[str]:
    """`baseline_paths` of a `visual_diff` hook whose baseline is frozen (the default once paths are set)."""
    if not isinstance(hook, dict) or hook.get("kind") != "visual_diff" or hook.get("frozen_baseline") is False:
        return []
    paths = hook.get("baseline_paths")
    return [p.strip() for p in paths] if _is_str_list(paths, non_empty_items=True) and paths else []


def _changed_status(root: Path, base_sha: Optional[str]) -> dict[str, str]:
    """path -> `A`/`M`/`D`/`T` for commits ``base_sha..HEAD`` plus the working tree (read-only probes)."""
    out: dict[str, str] = {}
    if base_sha:
        proc = _git_ro(root, ["diff", "--name-status", "-z", "--no-renames", base_sha, "HEAD"], timeout=60)
        if proc.returncode == 0:
            parts = proc.stdout.split("\0")
            for status, path in zip(parts[0::2], parts[1::2]):
                if path:
                    out[path] = status[:1]
    proc = _git_ro(root, ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames"], timeout=60)
    worktree: dict[str, str] = {}
    if proc.returncode == 0:
        for rec in proc.stdout.split("\0"):
            if len(rec) <= 3:
                continue
            xy, path = rec[:2], rec[3:]
            status = "A" if xy == "??" or "A" in xy else ("D" if "D" in xy else "M")
            prior = worktree.get(path)
            # A staged deletion plus an untracked/re-added file at the same path replaces an existing file.
            worktree[path] = status if prior is None or prior == status else "M"
    for path, status in worktree.items():
        prior = out.get(path)
        if prior == "A" and status == "D":
            out.pop(path)  # added after the base and deleted again: no net change to a frozen baseline
        elif prior == "A" or prior is None:
            out[path] = prior or status
        else:
            out[path] = "M" if status == "A" else status
    return out


def _existed_at(root: Path, ref: str, path: str) -> bool:
    return _git_ro(root, ["cat-file", "-e", f"{ref}:{path}"]).returncode == 0


def _git_blob_oid(data: bytes, oid_len: int) -> str:
    algo = hashlib.sha256 if oid_len == 64 else hashlib.sha1
    return algo(b"blob %d\0" % len(data) + data).hexdigest()


def _committed_blob_mismatches(root: Path, ref: str, globs: list[str]) -> set[str]:
    """Committed blobs at ``ref`` matching ``globs`` whose live file is missing, of another type or has other
    bytes. The file itself is hashed, so Git index flags (`assume-unchanged`, `skip-worktree`) and stat-cache
    tricks that hide a mutation from `git status` cannot. Filtered content (LFS, `autocrlf`) fails closed."""
    proc = _git_ro(root, ["ls-tree", "-r", "-z", "--full-tree", ref], timeout=60)
    if proc.returncode != 0:
        return set()
    out: set[str] = set()
    for rec in proc.stdout.split("\0"):
        meta, sep, rel = rec.partition("\t")
        fields = meta.split(" ")
        if not sep or len(fields) != 3 or fields[1] != "blob" or not _any_match(globs, rel):
            continue
        mode, _, oid = fields
        live = root / rel
        try:
            st = os.lstat(live)
            if mode == "120000":
                data = os.fsencode(os.readlink(live)) if stat.S_ISLNK(st.st_mode) else None
            else:
                data = live.read_bytes() if stat.S_ISREG(st.st_mode) else None
        except OSError:
            data = None
        if data is None or _git_blob_oid(data, len(oid)) != oid:
            out.add(rel)
    return out


def _frozen_baseline_changes(root: Path, frozen: dict[str, list[str]], base_sha: Optional[str]) -> list[dict[str, Any]]:
    """Existing frozen baseline files modified or deleted since ``base_sha`` (committed or in the working tree).
    Baseline files absent from ``base_sha`` (``HEAD`` when no base) are new surfaces, not tampering."""
    if not frozen:
        return []
    status = _changed_status(root, base_sha)
    ref = base_sha or "HEAD"
    out: list[dict[str, Any]] = []
    for name, globs in frozen.items():
        hits = sorted({
            *(p for p, st in status.items()
              if _any_match(globs, p) and (st != "A" or _existed_at(root, ref, p))),
            *_committed_blob_mismatches(root, ref, globs),
        })
        if hits:
            out.append({"name": name, "baseline_paths": globs, "changed_files": hits})
    return out


def _merge_baseline_changes(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for group in groups:
        for item in group:
            cur = merged.setdefault(item["name"], {**item, "changed_files": []})
            cur["changed_files"] = sorted({*cur["changed_files"], *item["changed_files"]})
    return list(merged.values())


# ---------------------------------------------------------------------------
# Fail-closed structural `visual_diff` command guard
# ---------------------------------------------------------------------------
# Proving that an arbitrary rewritten shell script keeps a committed threshold is undecidable, so the guard does
# not emulate the shell. An unchanged command (modulo whitespace, comments and harmless quoting) is always clean;
# an edited one passes only when it keeps the committed shell structure token for token and adds nothing but
# in-place threshold tightening or provably inert checker options. Everything else fails closed.

_THRESHOLD_KEY_RE = re.compile(
    r"threshold|toleran|max[-_]?diff|fuzz|max[-_]?pixel|diff[-_]?ratio|mismatch", re.IGNORECASE
)
_REGEN_FLAG_RE = re.compile(r"^--?(?:update|regen(?:erate)?|accept(?:-snapshots)?|snapshot[-_]?update|"
                            r"(?:force[-_]?)?update[-_]?(?:snapshots?|baselines?|goldens?)|"
                            r"overwrite[-_]?baselines?)$", re.IGNORECASE)
_REGEN_ENV_RE = re.compile(r"(?:^|_)(?:UPDATE_?SNAPSHOTS?|SNAPSHOTS?_?UPDATE|UPDATE_?BASELINES?|UPDATE_?GOLDENS?|"
                           r"REGENERATE)(?:_|$)", re.IGNORECASE)
_DISABLED = frozenset({"", "0", "false", "no", "off", "none"})
_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)
_PYTHON_RE = re.compile(r"^(?:python(?:\d+(?:\.\d+)*)?|pypy3?)$")
_SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "mksh", "ash", "busybox"})
# Segment heads that change the environment or control flow: only in-place threshold tightening is allowed.
_SEALED_HEADS = _SHELLS | frozenset({
    "export", "declare", "typeset", "local", "readonly", "unset", "env", "exec", "eval", "command", "builtin",
    "set", "shopt", "alias", "unalias", "trap", "source", ".", "if", "then", "elif", "else", "fi", "for",
    "while", "until", "do", "done", "case", "esac", "function", "!",
})
# The only options an edit may insert: output formatting/verbosity with fixed built-in values. Everything else
# (runner config, plugins, filters, modes, help/version, unknown flags) can skip or regenerate the visual check.
_INERT_EQ_OPTIONS = {
    "--color": frozenset({"always", "never", "auto", "yes", "no"}),
    "--reporter": frozenset({"list", "line", "dot", "json", "html", "junit", "github", "spec", "tap"}),
}
_INERT_TRAILING_FLAGS = frozenset({"--verbose", "--quiet", "--silent", "--no-color", "--color"})
# Short verbosity flags per runner; jest and vitest are absent because their `-v` is `--version` (exits 0).
_RUNNER_SHORT_FLAGS = {"pytest": frozenset({"-q", "-v"}), "go": frozenset({"-v"}), "cargo": frozenset({"-q", "-v"})}
_LAUNCHERS = frozenset({"npx", "pnpx", "bunx", "uvx", "uv", "pnpm", "yarn", "bun"})
_LAUNCHER_VERBS = frozenset({"run", "exec", "dlx", "x"})
# A shell word whose meaning is identical as an argv item and as unquoted shell text.
_PLAIN_WORD_RE = re.compile(r"[A-Za-z0-9_./:=+%,@-]+", re.ASCII)
_ASCII_THRESHOLD_RE = re.compile(r"^(?:(?:0|[1-9][0-9]*)(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$", re.ASCII)
# Command-launching wrappers that may sit between a segment head and a Python interpreter given `-u`.
_MAX_SCRIPT_DEPTH = 4
_OPERATORS = ("<<<", "&>>", "&&", "||", "|&", ";;", ">>", "<<", "<>", ">&", "<&", "&>", ">|",
              "|", ";", "&", "(", ")", ">", "<")
_UNVERIFIABLE = "mutates committed visual_diff command with an unverifiable shell/structural rewrite"


class _Tok(NamedTuple):
    text: str     # the word after quote removal, or the operator
    raw: str      # the source slice
    is_op: bool
    quoted: bool
    from_argv: bool = False  # an argv-list item: never seen by a shell

    @property
    def key(self) -> str:
        """Comparison identity: quoting is harmless unless the word expands (`'$HOME'` vs `$HOME`, `'*'`)."""
        return self.raw if re.search(r"[$*?\[~`{]", self.raw) else self.text


class _Slot(NamedTuple):
    """A threshold: `--flag val` / `--flag=val` (kind `flag`) or `VAR=val` (kind `env`), keyed by the exact
    spelling (dashes, case, `-` vs `_`: `--THRESHOLD` is another, unknown, flag); ``value`` is the ratio
    (`1%` -> 0.01) or None when not strict ASCII decimal syntax."""
    kind: str
    key: str
    raw: str
    value: Optional[float]
    percent: bool
    eq: bool


def _tokenize_visual_cmd(command: Any) -> list[_Tok]:
    """Shell words and operators of ``command`` (argv lists are literal words). Unquoted newlines are `;`,
    `#` at a word start comments to the end of the line, runs of `;` collapse and edge `;` are dropped, and a
    redirection keeps its fd (`2>`). Raises ValueError on an unclosed quote or a trailing lone backslash."""
    if isinstance(command, list):
        return [_Tok(str(c), str(c), False, True, True) for c in command]
    text = str(command or "")
    toks: list[_Tok] = []
    word: Optional[list[str]] = None
    i, n, start, quoted = 0, len(text), 0, False

    def flush(end: int) -> None:
        nonlocal word, quoted
        if word is not None:
            toks.append(_Tok("".join(word), text[start:end], False, quoted))
        word, quoted = None, False

    while i < n:
        ch = text[i]
        if ch in " \t\n":  # not `\r`: sh/bash keep it inside the word
            flush(i)
            if ch == "\n":
                toks.append(_Tok(";", "\n", True, False))
            i += 1
        elif word is None and ch == "#":
            end = text.find("\n", i)
            i = n if end < 0 else end
        elif word is None and ch in "{}" and (i + 1 == n or text[i + 1] in " \t\n;&|()"):
            toks.append(_Tok(ch, ch, True, False))
            i += 1
        elif ch == "\\" and text.startswith("\n", i + 1):
            i += 2  # line continuation
        elif op := next((o for o in _OPERATORS if text.startswith(o, i)), None):
            op_start, i = i, i + len(op)
            fd = "".join(word) if word is not None and not quoted else ""
            if re.fullmatch(r"\d+|\{[A-Za-z_]\w*\}", fd) and ("<" in op or ">" in op):
                word, op = None, fd + op  # `2>`: the fd belongs to the redirection
            flush(op_start)
            toks.append(_Tok(op, op, True, False))
        else:
            if word is None:
                word, start = [], i
            if ch == "\\":
                if i + 1 >= n:
                    raise ValueError("trailing backslash")
                word.append(text[i + 1])
                quoted, i = True, i + 2
            elif ch == "'":
                end = text.find("'", i + 1)
                if end < 0:
                    raise ValueError("unclosed single quote")
                word.append(text[i + 1:end])
                quoted, i = True, end + 1
            elif ch == '"':
                i += 1
                while i < n and text[i] != '"':
                    esc = text[i] == "\\" and i + 1 < n and text[i + 1] in '$`"\\\n'
                    word.append(text[i + 1] if esc and text[i + 1] != "\n" else "" if esc else text[i])
                    i += 2 if esc else 1
                if i >= n:
                    raise ValueError("unclosed double quote")
                quoted, i = True, i + 1
            else:
                word.append(ch)
                i += 1
    flush(n)
    out: list[_Tok] = []
    for tok in toks:
        if not (tok.is_op and tok.text == ";" and (not out or (out[-1].is_op and out[-1].text == ";"))):
            out.append(tok)
    while out and out[-1].is_op and out[-1].text == ";":
        out.pop()
    return out


def _segments(toks: list[_Tok]) -> tuple[list[str], list[list[_Tok]]]:
    """Operator sequence and the word segments around them (always ``len(ops) + 1`` segments)."""
    ops: list[str] = []
    segs: list[list[_Tok]] = [[]]
    for tok in toks:
        if tok.is_op:
            ops.append(tok.text)
            segs.append([])
        else:
            segs[-1].append(tok)
    return ops, segs


def _parse_value(raw: str) -> tuple[Optional[float], bool]:
    """Strict ASCII decimal/scientific syntax only: Python's `float` also takes `0.0_1`, `inf` and non-ASCII
    digits, which JS/Go/Rust checkers read as NaN or an error (and `0.8 > NaN` passes)."""
    pct = raw.endswith("%")
    number = raw[:-1] if pct else raw
    if not _ASCII_THRESHOLD_RE.match(number):
        return None, pct
    value = float(number)
    if not math.isfinite(value) or value < 0:
        return None, pct
    return (value / 100.0 if pct else value), pct


def _slot_items(words: list[str]) -> list[Any]:
    """``words`` with each threshold (`--flag val`, `--flag=val`, `VAR=val`) collapsed into one `_Slot`."""
    items: list[Any] = []
    i = 0
    while i < len(words):
        tok = words[i]
        m = _ASSIGN_RE.match(tok)
        if m and _THRESHOLD_KEY_RE.search(m.group(1)):
            items.append(_Slot("env", m.group(1), m.group(2), *_parse_value(m.group(2)), True))
        elif tok.startswith("-") and tok not in ("-", "--") and _THRESHOLD_KEY_RE.search(tok.partition("=")[0]):
            name, eq, raw = tok.partition("=")
            if not eq and i + 1 < len(words) and not words[i + 1].startswith("--"):
                i, raw = i + 1, words[i + 1]
            items.append(_Slot("flag", name, raw, *_parse_value(raw), bool(eq)))
        else:
            items.append(tok)
        i += 1
    return items


def _slot_ok(base: _Slot, live: _Slot) -> bool:
    """``live`` keeps ``base``'s bar: same slot, and the same text or a finite, comparable, no looser value."""
    if (base.kind, base.key) != (live.kind, live.key):
        return False
    if live.raw == base.raw:
        return True
    if base.value is None or live.value is None:
        return False
    if base.percent != live.percent:
        return False  # checkers read `1%` and `0.01` differently (`Number("1%")` is NaN): keep the committed unit
    return live.value <= base.value


def _item_ok(base: Any, live: Any) -> bool:
    if isinstance(base, _Slot) and isinstance(live, _Slot):
        return _slot_ok(base, live)
    return not isinstance(base, _Slot) and not isinstance(live, _Slot) and base == live


def _is_assignment(item: Any) -> bool:
    return item.kind == "env" if isinstance(item, _Slot) else bool(_ASSIGN_RE.match(item))


def _is_regen(tok: str, prev: Optional[str]) -> bool:
    """An active snapshot-regeneration option or environment assignment (`=false`/`=0`/... disable it)."""
    m = _ASSIGN_RE.match(tok)
    if m:
        return bool(_REGEN_ENV_RE.search(m.group(1))) and m.group(2).strip().lower() not in _DISABLED
    name, eq, value = tok.partition("=")
    if _REGEN_FLAG_RE.match(name):
        return not (eq and value.strip().lower() in _DISABLED)
    word = name[1:]
    if not name.startswith("-") or eq or not word.isalpha() or "u" not in word or len(word) > 3:
        return False
    prev_name = os.path.basename(prev or "")
    exempt = bool(_PYTHON_RE.match(prev_name)) or prev_name in _SHELLS | {"set", "env", "diff"}
    return not exempt and word.lower() not in {"run", "sub", "cpu", "out", "url", "num"}


def _runner(live: list[Any], head: int) -> Optional[str]:
    """The command a segment runs: its head, or the tool after `npx`/`uvx`/`uv run`/`pnpm exec`/`yarn`/..."""
    k = head
    while k < len(live) and isinstance(live[k], str) and (
            os.path.basename(live[k]) in _LAUNCHERS or (k > head and live[k] in _LAUNCHER_VERBS)):
        k += 1
    word = live[k] if k < len(live) else None
    return os.path.basename(word) if isinstance(word, str) and not word.startswith("-") else None


def _insert_ok(live: list[Any], j: int, head: int, trailing_from: int, matched: set[int]) -> bool:
    """Whether item ``live[j]``, absent from the committed segment, is on the positive allowlist of inert
    insertions: a disabled regeneration assignment before the head; after it a disabled regeneration option
    (`--update-snapshots=false`) or `--color=`/`--reporter=` with a fixed built-in value; after every committed
    word (``trailing_from``) a verbosity/color flag (`-q`/`-v` only for runners where they are); or Python's
    unbuffered `-u` right after a committed interpreter that heads the segment and before its committed script.
    Nothing may follow a bare option, which could swallow it as its value (`-run --color=never`)."""
    item = live[j]
    prev = live[j - 1] if j > 0 else None
    if j > head and isinstance(prev, str) and prev.startswith("-") and "=" not in prev:
        return False
    if isinstance(item, _Slot):
        return False  # committed thresholds are only tightened in place; a new one (even `0`/`0%`) fails closed
    if _is_assignment(item):
        name, _, value = item.partition("=")
        return j < head and bool(_REGEN_ENV_RE.search(name)) and value.lower() in _DISABLED
    if j <= head:
        return False
    name, eq, value = item.partition("=")
    if eq:
        if _REGEN_FLAG_RE.match(name):
            return value.lower() in _DISABLED
        return value in _INERT_EQ_OPTIONS.get(name, ())
    if item == "-u" and j - 1 == head and j + 1 in matched and _is_python_exe(live[head]):
        return True  # behind a wrapper `python3` may be a module/tool name or an option value (`uv run -m python3`)
    if j < trailing_from:
        return False
    return item in _INERT_TRAILING_FLAGS or item in _RUNNER_SHORT_FLAGS.get(_runner(live, head) or "", ())


def _is_bare_or_absolute(word: Any) -> bool:
    """A command resolved via PATH (`python3`) or an absolute path, never a repo-relative script (`tests/python3`)."""
    return isinstance(word, str) and ("/" not in word or word.startswith("/"))


def _is_python_exe(word: Any) -> bool:
    return _is_bare_or_absolute(word) and bool(_PYTHON_RE.match(os.path.basename(word)))


def _outer_expands(tok: _Tok) -> bool:
    """Whether the outer shell expands part of ``tok`` (`$`/`` ` `` unquoted or in double quotes; globs, `~` and
    braces unquoted), so the word a nested `sh -c` receives is not its literal text."""
    if tok.from_argv:
        return False
    raw, i, dq = tok.raw, 0, False
    while i < len(raw):
        ch = raw[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "'" and not dq:
            i = raw.index("'", i + 1) + 1
            continue
        if ch == '"':
            dq = not dq
        elif ch in "$`" or (not dq and ch in "*?[~{"):
            return True
        i += 1
    return False


def _segment_ok(base_toks: list[_Tok], live_toks: list[_Tok], depth: int, sealed: bool, trailing: bool) -> bool:
    base, live = [t.key for t in base_toks], [t.key for t in live_toks]
    if base == live:
        return True
    b_items, l_items = _slot_items(base), _slot_items(live)
    head = next((k for k, t in enumerate(base) if not _ASSIGN_RE.match(t)), None)
    name = os.path.basename(base[head]) if head is not None else None
    if name in _SHELLS and len(base) == len(live):
        # an unchanged `sh|bash ... -c SCRIPT [args]` wrapper: only the script may differ, checked recursively
        diff = [k for k in range(len(base)) if base[k] != live[k]]
        if len(diff) == 1 and diff[0] > head + 1 and re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", base[diff[0] - 1]):
            # the outer shell expands `$T`/globs in a double-quoted or unquoted script before the child sees it, so
            # neither side may expand: any quoting flip that changes what the child shell receives fails closed
            return (not _outer_expands(base_toks[diff[0]]) and not _outer_expands(live_toks[diff[0]])
                    and all(re.fullmatch(r"[-+][A-Za-z]+", t) and "c" not in t for t in base[head + 1:diff[0] - 1])
                    and _structure_ok(base_toks[diff[0]].text, live_toks[diff[0]].text, depth + 1))
    if sealed or head is None or name in _SEALED_HEADS:
        return len(b_items) == len(l_items) and all(map(_item_ok, b_items, l_items))
    matched: list[int] = []
    j = 0
    for item in b_items:  # every committed word and threshold, in order (greedy earliest match)
        while j < len(l_items) and not _item_ok(item, l_items[j]):
            j += 1
        if j >= len(l_items):
            return False
        matched.append(j)
        j += 1
    live_head = matched[next(k for k, t in enumerate(b_items) if not _is_assignment(t))]
    trailing_from = matched[-1] + 1 if trailing else len(l_items)
    return all(_insert_ok(l_items, k, live_head, trailing_from, set(matched))
               for k in range(len(l_items)) if k not in matched)


def _is_redirect(op: str) -> bool:
    return "<" in op or ">" in op


def _structure_ok(base_cmd: Any, live_cmd: Any, depth: int = 0) -> bool:
    """The fail-closed structural rule: identical operators and segment count, each segment aligned 1-to-1."""
    if depth > _MAX_SCRIPT_DEPTH:
        return False
    try:
        base, live = _tokenize_visual_cmd(base_cmd), _tokenize_visual_cmd(live_cmd)
    except ValueError:
        return False
    if isinstance(base_cmd, list) != isinstance(live_cmd, list) and not all(
            not t.is_op and _PLAIN_WORD_RE.fullmatch(t.text) for t in (*base, *live)):
        return False  # argv list <-> shell string: only plain words mean the same thing in both
    if [(t.key, t.is_op) for t in base] == [(t.key, t.is_op) for t in live]:
        return True
    (b_ops, b_segs), (l_ops, l_segs) = _segments(base), _segments(live)
    if b_ops != l_ops or any(op.lstrip("0123456789").startswith("<<") and "<<<" not in op for op in b_ops):
        return False  # a changed heredoc body is code, not argv
    # a redirection target is sealed, and nothing may trail a word that precedes a redirection (`cmd 2>f`)
    return all(_segment_ok(b, s, depth, k > 0 and _is_redirect(b_ops[k - 1]),
                           k == len(b_ops) or not _is_redirect(b_ops[k]))
               for k, (b, s) in enumerate(zip(b_segs, l_segs)))


def _flat_segments(command: Any, depth: int = 0) -> tuple[list[list[str]], int]:
    """Word segments of ``command`` and of every nested `sh -c` script, plus its `||` count (diagnostics)."""
    try:
        ops, segs = _segments(_tokenize_visual_cmd(command))
    except ValueError:
        return [], 0
    out, ors = [[t.text for t in s] for s in segs if s], ops.count("||")
    for seg in out[:]:
        if depth < _MAX_SCRIPT_DEPTH and os.path.basename(seg[0]) in _SHELLS:
            k = next((k for k in range(1, len(seg) - 1) if re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", seg[k])), None)
            if k is not None:
                inner, inner_ors = _flat_segments(seg[k + 1], depth + 1)
                out, ors = out + inner, ors + inner_ors
    return out, ors


def _threshold_reasons(base_segs: list[list[str]], live_segs: list[list[str]]) -> list[str]:
    def slots(segs: list[list[str]]) -> dict[tuple[str, str], list[_Slot]]:
        out: dict[tuple[str, str], list[_Slot]] = {}
        for item in (i for seg in segs for i in _slot_items(seg) if isinstance(i, _Slot)):
            out.setdefault((item.kind, item.key), []).append(item)
        return out

    base, live = slots(base_segs), slots(live_segs)
    reasons: list[str] = []
    for (kind, key), b_vals in base.items():
        l_vals = live.get((kind, key), [])
        label = f"`{key}`" + (" environment variable" if kind == "env" else "")
        if not l_vals:
            reasons.append(f"removes threshold `{key}={b_vals[0].raw}` (falls back to the tool default)")
            continue
        if len(l_vals) < len(b_vals):
            reasons.append(f"removes {len(b_vals) - len(l_vals)} of {len(b_vals)} {label} threshold(s)")
        strictest = min(b_vals, key=lambda s: math.inf if s.value is None else s.value)
        pairs = [*zip(b_vals, l_vals), *((strictest, s) for s in l_vals[len(b_vals):])]
        bad = [(b, s) for b, s in pairs if not _slot_ok(b, s)]
        if any(s.value is None for _, s in bad):
            reasons.append(f"sets non-finite or unparseable threshold {label} "
                           f"({', '.join(s.raw for _, s in bad if s.value is None)})")
        if any(s.value is not None and s.percent != b.percent for b, s in bad):
            reasons.append(f"changes threshold unit {label} (`%` vs plain number) from "
                           f"{', '.join(s.raw for s in b_vals)} to {', '.join(s.raw for s in l_vals)}")
        if any(s.value is not None and s.percent == b.percent for b, s in bad):
            reasons.append(f"loosens threshold {label} from {', '.join(s.raw for s in b_vals)} to "
                           f"{', '.join(s.raw for s in l_vals)}")
    for (kind, key), l_vals in live.items():
        extra = l_vals[len(base.get((kind, key), [])):]
        if extra:
            reasons.append(f"adds threshold `{key}={extra[0].raw}` "
                           "(committed thresholds may only be tightened in place)")
    return reasons


def _visual_hook_loosening(base_hook: dict[str, Any], live_hook: dict[str, Any]) -> list[str]:
    """Ways ``live_hook`` weakens the committed `visual_diff` ``base_hook`` (empty when it does not)."""
    reasons: list[str] = []
    base_cmd, live_cmd = base_hook.get("command"), live_hook.get("command")
    unchanged = isinstance(base_cmd, str) and isinstance(live_cmd, str) and base_cmd.strip() == live_cmd.strip()
    if not unchanged:
        try:
            _tokenize_visual_cmd(live_cmd)
        except ValueError:
            reasons.append("mutates visual_diff command with unparseable shell syntax")
    if not unchanged and not reasons and not _structure_ok(base_cmd, live_cmd):
        (base_segs, base_ors), (live_segs, live_ors) = _flat_segments(base_cmd), _flat_segments(live_cmd)

        def regen(segs: list[list[str]]) -> Counter[str]:
            return Counter(t for seg in segs for k, t in enumerate(seg) if _is_regen(t, seg[k - 1] if k else None))

        added = sorted((regen(live_segs) - regen(base_segs)).elements())
        if added:
            reasons.append("adds snapshot-regeneration flag(s) " + ", ".join(f"`{f}`" for f in added))
        reasons.extend(_threshold_reasons(base_segs, live_segs))
        if live_ors > base_ors:
            reasons.append("adds shell `||` fallback to visual_diff hook (a failing check can still pass)")
        reasons.append(_UNVERIFIABLE)
    base_frozen = _frozen_baseline_globs(base_hook)
    if base_frozen:
        live_frozen = _frozen_baseline_globs(live_hook) if live_hook.get("kind") == "visual_diff" else []
        if not live_frozen:
            reasons.append("no longer freezes its baseline (`frozen_baseline`/`baseline_paths` removed)")
        else:
            dropped = [g for g in base_frozen if g not in live_frozen]
            if dropped:
                reasons.append("drops frozen baseline glob(s) " + ", ".join(f"`{g}`" for g in dropped))
    return reasons


# ---------------------------------------------------------------------------
# Gate-loosening detection (read-only)
# ---------------------------------------------------------------------------


def _baseline_gates(root: Path, sha: str) -> Optional[dict[str, Any]]:
    """The effective hooks + Doctor commands committed at ``sha``, or None when no harness is committed there.

    Uses discovery's per-field fallback, so a committed harness.json without `hooks` (or `phases`) still
    inherits the required gates declared in the committed verify.md."""
    json_proc = _git_ro(root, ["show", f"{sha}:{HARNESS_DIR_NAME}/{HARNESS_JSON_NAME}"])
    manifest: Optional[dict[str, Any]] = None
    if json_proc.returncode == 0:
        try:
            loaded = json.loads(json_proc.stdout)
        except ValueError:
            loaded = None
        if isinstance(loaded, dict):
            manifest = loaded
    md_proc = _git_ro(root, ["show", f"{sha}:{HARNESS_DIR_NAME}/{VERIFY_MD_NAME}"])
    md_sections = _split_sections(md_proc.stdout) if md_proc.returncode == 0 else None
    if manifest is None and md_sections is None:
        return None
    hooks, phases = _effective_hooks_and_phases(manifest, md_sections)
    return {
        "source": HARNESS_JSON_NAME if manifest is not None else VERIFY_MD_NAME,
        "hooks": hooks,
        "doctor": list(phases.get("doctor") or []),
    }


def detect_harness_gate_loosening(repo_path: str | Path, base_ref: Optional[str] = None) -> dict[str, Any]:
    """Strictly read-only: flag required hooks removed or downgraded to optional, dropped Doctor commands,
    `visual_diff` hooks mutated to regenerate baselines or loosen thresholds (or unfreeze them), and frozen
    baseline files modified since ``base_ref`` (default ``HEAD``: working-tree edits only), comparing the live
    `.factonio/` with the harness committed at ``base_ref``."""
    root = Path(repo_path).expanduser()
    result: dict[str, Any] = {
        "checked": False,
        "loosened": False,
        "base_ref": base_ref,
        "base_commit": None,
        "baseline_source": None,
        "removed_required_hooks": [],
        "downgraded_hooks": [],
        "removed_doctor_commands": [],
        "loosened_visual_hooks": [],
        "visual_baseline_changes": [],
        "findings": [],
    }
    ref = _validate_ref(base_ref) if base_ref is not None else None
    if not _is_git_repo(root):
        if ref is not None:
            raise ValueError(f"base_ref {ref!r} given but {root} is not a git repository")
        result["reason"] = "not a git repository"
        return result
    sha = _resolve_commit(root, ref) if ref is not None else _head_commit(root)
    if ref is not None and not sha:
        raise ValueError(f"base_ref {ref!r} does not resolve to a commit in {root}")
    if not sha:
        result["reason"] = "no HEAD commit"
        return result
    result["base_commit"] = sha
    baseline = _baseline_gates(root, sha)
    live = discover_harness(root)
    live_hooks, live_doctor = live["hooks"], list(live["phases"].get("doctor") or [])
    if baseline is None:
        result["reason"] = f"no {HARNESS_DIR_NAME}/ harness committed at {sha[:12]}"
        # A first, not-yet-committed harness has no committed gates to compare, but its frozen baselines that
        # already exist at ``sha`` are still protected (`baseline_source: live`).
        if not any(_frozen_baseline_globs(h) for h in live_hooks
                   if isinstance(h, dict) and str(h.get("name") or "").strip()):
            return result
        baseline = {"source": "live", "hooks": [], "doctor": []}
    result["checked"] = True
    result["baseline_source"] = baseline["source"]

    live_by_name = {str(h.get("name") or "").strip(): h for h in live_hooks}
    short = sha[:12]
    seen: set[str] = set()
    for hook in baseline["hooks"]:
        name = str(hook.get("name") or "").strip()
        if not name or name in seen or hook.get("optional", False) is not False:
            continue
        seen.add(name)
        current = live_by_name.get(name)
        if current is None:
            result["removed_required_hooks"].append(name)
            result["findings"].append(f"Gate loosening: required hook {name!r} (required at {short}) was removed")
        elif current.get("optional", False) is not False:
            result["downgraded_hooks"].append(name)
            result["findings"].append(
                f"Gate loosening: required hook {name!r} was downgraded to optional (required at {short})"
            )
    live_doctor_set = {c.strip() for c in live_doctor}
    for cmd in baseline["doctor"]:
        if cmd.strip() not in live_doctor_set and cmd not in result["removed_doctor_commands"]:
            result["removed_doctor_commands"].append(cmd)
            result["findings"].append(f"Gate loosening: Doctor command `{cmd}` (present at {short}) was removed")
    frozen: dict[str, list[str]] = {}
    for hook in baseline["hooks"]:
        name = str(hook.get("name") or "").strip()
        if not name or hook.get("kind") != "visual_diff":
            continue
        current = live_by_name.get(name)
        reasons = _visual_hook_loosening(hook, current) if current is not None else []
        if reasons and name not in result["loosened_visual_hooks"]:
            result["loosened_visual_hooks"].append(name)
            result["findings"].extend(
                f"Gate loosening: visual_diff hook {name!r} {why} (vs {short})" for why in reasons
            )
    # A live hook may freeze more files than the committed one (or be new): its existing baselines count too.
    frozen = _collect_frozen_globs(baseline["hooks"], live_hooks)
    result["visual_baseline_changes"] = _frozen_baseline_changes(root, frozen, sha)
    for change in result["visual_baseline_changes"]:
        files = ", ".join(change["changed_files"][:5])
        result["findings"].append(
            f"Gate loosening: frozen baseline of visual_diff hook {change['name']!r} was modified since {short}: {files}"
        )
    result["loosened"] = bool(result["findings"])
    return result


def _collect_frozen_globs(committed_hooks: Iterable[Any], live_hooks: Iterable[Any]) -> dict[str, list[str]]:
    """hook name -> frozen `baseline_paths` of the committed hooks unioned with the live ones."""
    frozen: dict[str, list[str]] = {}
    for hook in [*committed_hooks, *live_hooks]:
        name = str(hook.get("name") or "").strip() if isinstance(hook, dict) else ""
        globs = _frozen_baseline_globs(hook) if name else []
        if globs:
            frozen[name] = list(dict.fromkeys([*frozen.get(name, []), *globs]))
    return frozen


def _pre_execution_frozen_contract(root: Path, gate: dict[str, Any],
                                   live_hooks: list[Any]) -> tuple[Optional[str], dict[str, list[str]]]:
    """The base commit and effective frozen globs (committed union live) fixed before Doctor/hooks run."""
    sha = gate.get("base_commit")
    if not sha:
        return None, {}
    committed = _baseline_gates(root, sha)
    return sha, _collect_frozen_globs((committed or {}).get("hooks") or [], live_hooks)


def _frozen_contract_gate(root: Path, sha: str, frozen: dict[str, list[str]],
                          base_ref: Optional[str]) -> dict[str, Any]:
    """Re-check the pre-execution frozen contract after Doctor/hooks ran, whatever they did to `.factonio/`: a
    frozen hook removed or unfrozen meanwhile is flagged, and its baselines are still compared with ``sha``."""
    result: dict[str, Any] = {
        "checked": True, "loosened": False, "base_ref": base_ref, "base_commit": sha,
        "baseline_source": "pre-execution", "removed_required_hooks": [], "downgraded_hooks": [],
        "removed_doctor_commands": [], "loosened_visual_hooks": [], "visual_baseline_changes": [], "findings": [],
    }
    now = _collect_frozen_globs([], discover_harness(root)["hooks"])
    for name, globs in frozen.items():
        dropped = [g for g in globs if g not in now.get(name, [])]
        if dropped:
            result["loosened_visual_hooks"].append(name)
            result["findings"].append(
                f"Gate loosening: frozen visual_diff hook {name!r} was removed or unfrozen while Doctor/hooks ran "
                f"(baseline glob(s) {', '.join(f'`{g}`' for g in dropped)} no longer frozen)"
            )
    result["visual_baseline_changes"] = _frozen_baseline_changes(root, frozen, sha)
    for change in result["visual_baseline_changes"]:
        files = ", ".join(change["changed_files"][:5])
        result["findings"].append(
            f"Gate loosening: frozen baseline of visual_diff hook {change['name']!r} was modified since "
            f"{sha[:12]}: {files}"
        )
    result["loosened"] = bool(result["findings"])
    return result


# ---------------------------------------------------------------------------
# Read-only drift audit
# ---------------------------------------------------------------------------


def _audit_base(root: Path, base_ref: Optional[str], manifest: Optional[dict[str, Any]]) -> tuple[Optional[str], str]:
    if base_ref:
        ref = _validate_ref(base_ref)
        sha = _resolve_commit(root, ref)
        if not sha:
            raise ValueError(f"base_ref {ref!r} does not resolve to a commit in {root}")
        return sha, "base_ref"
    recorded = (manifest or {}).get("base_commit")
    if isinstance(recorded, str) and recorded.strip():
        sha = _resolve_commit(root, recorded.strip())
        if sha:
            return sha, "harness.json base_commit"
    proc = _git_ro(root, ["log", "-1", "--format=%H", "--", HARNESS_DIR_NAME])
    sha = proc.stdout.strip()
    if proc.returncode == 0 and sha:
        return sha, f"last commit touching {HARNESS_DIR_NAME}/"
    return None, "none"


def _changed_files(root: Path, base_sha: Optional[str], *, include_harness_dir: bool = False) -> list[str]:
    changed: set[str] = set()
    if base_sha:
        proc = _git_ro(root, ["diff", "--name-only", "-z", "--no-renames", base_sha, "HEAD"], timeout=60)
        if proc.returncode == 0:
            changed.update(p for p in proc.stdout.split("\0") if p)
    proc = _git_ro(root, ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames"], timeout=60)
    if proc.returncode == 0:
        for rec in proc.stdout.split("\0"):
            if len(rec) > 3:
                changed.add(rec[3:])
    if include_harness_dir:
        return sorted(changed)
    return sorted(p for p in changed if not (p == HARNESS_DIR_NAME or p.startswith(HARNESS_DIR_NAME + "/")))


def audit_harness(repo_spec: str, *, base_ref: Optional[str] = None) -> dict[str, Any]:
    """Strictly read-only drift audit of the repo harness against the live repository."""
    target = _resolve_repo(repo_spec)
    root: Path = target["path"]
    info = discover_harness(root)
    result: dict[str, Any] = {
        "ok": True,
        "repo": target["repo"],
        "repo_path": str(root),
        "present": info["present"],
        "mode": info["mode"],
        "base_ref": base_ref,
        "base_commit": None,
        "base_source": None,
        "head_commit": None,
        "changed_files": [],
        "uncovered_changed_files": [],
        "stale_features": [],
        "stale_feature_details": [],
        "orphaned_globs": [],
        "orphaned_glob_details": [],
        "new_routes": [],
        "removed_routes": [],
        "new_cli_commands": [],
        "removed_cli_commands": [],
        "stale_invariants": [],
        "dead_cover_globs": [],
        "unresolved_sync_commits": [],
        "visual_baseline_changes": [],
        "warnings": [],
        "manifest_errors": [],
        "recommendations": [],
        "gate_loosening": _unchecked_gate(base_ref),
    }
    if not info["present"]:
        gate = detect_harness_gate_loosening(root, base_ref) if _is_git_repo(root) else _unchecked_gate(base_ref)
        recs = [
            f"No {HARNESS_DIR_NAME}/{VERIFY_MD_NAME}: run `hermes factonio harness init {target['repo']}` "
            "(or `factonio_bootstrap_verify` action=init) and commit the result."
        ]
        if gate["loosened"]:
            result["warnings"].extend(gate["findings"])
            recs.append(_GATE_LOOSENING_RECOMMENDATION)
        result.update(
            drifted=True,
            severity="missing",
            recommendations=recs,
            gate_loosening=gate,
            visual_baseline_changes=list(gate.get("visual_baseline_changes") or []),
        )
        result["drift_classification"] = classify_harness_drift(result)
        return result

    manifest = info["manifest"] if not info["manifest_error"] else None
    if info["manifest_error"]:
        result["manifest_errors"] = [info["manifest_error"]]
    elif manifest is not None:
        result["manifest_errors"] = _validate_manifest(manifest)
    result["manifest_errors"].extend(_verify_md_hook_errors(info))
    result["warnings"].extend(result["manifest_errors"])
    is_git = _is_git_repo(root)
    if not is_git:
        result["warnings"].append("Not a git repository: change-based drift checks skipped")
        if base_ref:
            raise ValueError(f"base_ref {base_ref!r} given but {root} is not a git repository")
        base_sha, base_source = None, "none"
    else:
        base_sha, base_source = _audit_base(root, base_ref, manifest)
        result["head_commit"] = _head_commit(root)
        if base_sha is None:
            result["warnings"].append("No base commit found; only uncommitted changes are considered")
    result["base_commit"] = base_sha
    result["base_source"] = base_source

    changed = _changed_files(root, base_sha) if is_git else []
    result["changed_files"] = changed
    raw_features = (manifest or {}).get("feature_map")
    features = [f for f in raw_features if isinstance(f, dict)] if isinstance(raw_features, list) else []
    all_globs = [p for f in features for p in _str_list(f.get("paths"))]
    source_changed = [p for p in changed if Path(p).suffix.lower() in SOURCE_EXTS]
    result["uncovered_changed_files"] = [p for p in source_changed if not _any_match(all_globs, p)]

    for feat in features:
        hits = [p for p in changed if _any_match(_str_list(feat.get("paths")), p)]
        if hits:
            result["stale_features"].append(str(feat.get("id")))
            result["stale_feature_details"].append(
                {"id": str(feat.get("id")), "surface": feat.get("surface"), "changed_files": hits}
            )

    files, _ = _list_repo_files(root)
    dead = _dead_globs(manifest, files)
    result["orphaned_glob_details"] = dead
    result["orphaned_globs"] = sorted({d["glob"] for d in dead})

    surfaces = (manifest or {}).get("discovered_surfaces")
    if (
        isinstance(surfaces, dict)
        and _is_str_list(surfaces.get("routes") or [])
        and _is_str_list(surfaces.get("cli_commands") or [])
    ):
        profile = detect_repo_profile(root)
        old_routes = set(_str_list(surfaces.get("routes")))
        old_cli = set(_str_list(surfaces.get("cli_commands")))
        live_routes, live_cli = set(profile["routes"]), set(profile["cli_commands"])
        result["new_routes"] = sorted(live_routes - old_routes)
        result["removed_routes"] = sorted(old_routes - live_routes)
        result["new_cli_commands"] = sorted(live_cli - old_cli)
        result["removed_cli_commands"] = sorted(old_cli - live_cli)
    else:
        result["warnings"].append("No harness.json discovered_surfaces baseline: route/CLI drift not computed")

    formal = _audit_formal_invariants(
        root, info["hooks"], files=_invariant_files(root), harness_base=base_sha, harness_base_source=base_source,
        is_git=is_git,
    )
    result["formal_invariants"] = formal
    for key in ("stale_invariants", "dead_cover_globs", "unresolved_sync_commits"):
        result[key] = formal[key]
    if formal["manifest_errors"]:
        result["manifest_errors"].extend(formal["manifest_errors"])
        result["warnings"].extend(formal["manifest_errors"])

    recs: list[str] = result["recommendations"]
    if result["uncovered_changed_files"]:
        recs.append(
            f"Add feature_map entries covering {len(result['uncovered_changed_files'])} changed source file(s): "
            + ", ".join(result["uncovered_changed_files"][:5])
        )
    if result["stale_features"]:
        recs.append(
            "Review Drive/Feature Map scenarios for changed feature(s): " + ", ".join(result["stale_features"])
        )
    if result["orphaned_globs"]:
        recs.append("Remove or fix dead feature_map globs: " + ", ".join(result["orphaned_globs"]))
    if result["new_routes"] or result["removed_routes"]:
        recs.append(
            "Update Drive HTTP probes (new: " + (", ".join(result["new_routes"]) or "-")
            + "; removed: " + (", ".join(result["removed_routes"]) or "-") + ")"
        )
    if result["new_cli_commands"] or result["removed_cli_commands"]:
        recs.append(
            "Update Drive CLI checks (new: " + (", ".join(result["new_cli_commands"]) or "-")
            + "; removed: " + (", ".join(result["removed_cli_commands"]) or "-") + ")"
        )
    if result["stale_invariants"]:
        recs.append(
            "Re-prove formal invariant(s) "
            + ", ".join(i["name"] for i in result["stale_invariants"])
            + " against their changed covered files, then bump `sync_commit` to the re-proven commit."
        )
    if result["dead_cover_globs"]:
        recs.append(
            "Fix or remove dead formal-invariant `covers` globs: "
            + ", ".join(f"{d['hook']}:{d['glob']}" for d in result["dead_cover_globs"])
        )
    if result["unresolved_sync_commits"]:
        recs.append(
            "Point `sync_commit` at a commit reachable in this checkout (squash merges rewrite SHAs): "
            + ", ".join(u["name"] for u in result["unresolved_sync_commits"])
        )
    doc_drifted = any(
        result[k]
        for k in (
            "uncovered_changed_files", "stale_features", "orphaned_globs",
            "new_routes", "removed_routes", "new_cli_commands", "removed_cli_commands",
            "stale_invariants", "dead_cover_globs",
        )
    )
    if doc_drifted:
        recs.append(
            f"Refresh with `hermes factonio harness init {target['repo']} --force` (hooks are preserved) "
            "or edit .factonio/ by hand, then commit it so the audit baseline moves forward."
        )
    gate = detect_harness_gate_loosening(root, base_sha) if base_sha else _unchecked_gate(base_ref)
    tried = {base_sha}
    live_gate: Optional[dict[str, Any]] = None  # a `baseline_source: live` result compares no committed gates

    def committed(g: dict[str, Any]) -> bool:
        return bool(g["checked"]) and g.get("baseline_source") != "live"

    # A generated `base_commit` usually predates `.factonio/` itself: gate against the last committed harness,
    # but only then, so a pinned approved harness stays the bar for later commits that loosen it.
    for step in ("fallback", "head"):
        if committed(gate):
            break
        if gate["checked"] and live_gate is None:
            live_gate = gate
        if step == "fallback":
            nxt = _frozen_baseline_base(root, base_ref, base_sha) if is_git else None
        else:
            nxt = result["head_commit"]
        if nxt and nxt not in tried:
            tried.add(nxt)
            gate = detect_harness_gate_loosening(root, nxt)
    if live_gate is not None and live_gate is not gate:
        gate = _merge_gate(gate, live_gate) if gate["checked"] else live_gate
    result["gate_loosening"] = gate
    if gate["loosened"]:
        result["warnings"].extend(gate["findings"])
        recs.append(_GATE_LOOSENING_RECOMMENDATION)
    if is_git:
        frozen = {str(h.get("name")): _frozen_baseline_globs(h) for h in info["hooks"] if _frozen_baseline_globs(h)}
        result["visual_baseline_changes"] = _merge_baseline_changes(
            list(gate.get("visual_baseline_changes") or []),
            _frozen_baseline_changes(root, frozen, _frozen_baseline_base(root, base_ref, base_sha)),
        )
    if result["visual_baseline_changes"]:
        recs.append(_FROZEN_BASELINE_RECOMMENDATION)
    if result["manifest_errors"]:
        recs.append(
            f"Repair the malformed {HARNESS_DIR_NAME}/ manifest or hooks ({len(result['manifest_errors'])} "
            f"error(s)); `hermes factonio harness check {target['repo']}` lists them."
        )
    drifted = (
        doc_drifted or gate["loosened"] or bool(result["manifest_errors"]) or bool(result["visual_baseline_changes"])
    )
    result["drifted"] = drifted
    result["severity"] = "stale" if drifted else "clean"
    classification = classify_harness_drift(result)
    result["drift_classification"] = classification
    if classification["classification"] != "clean":
        recs.append(f"Drift classification `{classification['classification']}`: {classification['recommended_action']}")
    return result


_DRIFT_PRIORITY: tuple[str, ...] = ("product_regression", "harness_gap", "doc_drift")
_DRIFT_ACTIONS: dict[str, str] = {
    "clean": "No action: the harness matches the repository and every live gate that ran passed.",
    "doc_drift": (
        "Fix `.factonio/` (refresh with `hermes factonio harness init <repo> --force` or edit the Feature Map, "
        "Drive and Hooks by hand) and commit it; restore any removed or downgraded required gate."
    ),
    "harness_gap": (
        "Fix the harness helper or environment (a malformed `.factonio/harness.json`, Doctor prerequisites, hook "
        "command paths, timeouts) so the declared gates can run; do not edit product code or weaken gates to get green."
    ),
    "product_regression": (
        "Report a product bug (e.g. a scoped `[implement]` fix card); never edit `.factonio/` to paper over "
        "broken product behavior."
    ),
}
# Exit codes that mean "the gate could not run" (shell: not executable / command not found), not "the gate failed".
_ENV_EXIT_CODES = frozenset({126, 127})
_GATE_LOOSENING_RECOMMENDATION = (
    "Restore the removed or downgraded required gates in .factonio/ (hooks and Doctor commands); loosening a "
    "gate needs explicit spec authorization and a `factonio_log_decision` countersign."
)


_FROZEN_BASELINE_RECOMMENDATION = (
    "Restore the modified frozen visual baselines; regenerating a frozen baseline (or loosening a visual threshold) "
    "to turn a failing visual_diff green needs explicit spec/design approval and a `factonio_log_decision` countersign."
)


def _frozen_baseline_base(root: Path, base_ref: Optional[str], base_sha: Optional[str]) -> Optional[str]:
    """Audit baseline for frozen files: ``base_ref`` when given, else the last commit touching `.factonio/`
    (baselines committed together with the harness are the frozen bar), else the audit base."""
    if base_ref:
        return base_sha
    proc = _git_ro(root, ["log", "-1", "--format=%H", "--", HARNESS_DIR_NAME])
    sha = proc.stdout.strip()
    return sha if proc.returncode == 0 and sha else base_sha


def _unchecked_gate(base_ref: Optional[str]) -> dict[str, Any]:
    return {
        "checked": False, "loosened": False, "base_ref": base_ref, "base_commit": None, "baseline_source": None,
        "removed_required_hooks": [], "downgraded_hooks": [], "removed_doctor_commands": [],
        "loosened_visual_hooks": [], "visual_baseline_changes": [], "findings": [],
    }


def classify_harness_drift(
    audit_result: Optional[dict[str, Any]], check_result: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    """Classify audit/check findings as `clean`, `doc_drift`, `harness_gap`, or `product_regression`.

    `doc_drift`: `.factonio/` no longer describes the repo (missing/stale harness, section errors, dead globs,
    stale formal invariants, loosened gates or tampered frozen baselines). `harness_gap`: the harness itself is
    broken or a gate could not run (malformed harness.json: invalid JSON, schema/`schema_version` errors or an
    unresolved `sync_commit`; Doctor failure, hook timeout or command-not-found). `product_regression`: a required hook ran and failed. Priority:
    product_regression > harness_gap > doc_drift > clean.
    """
    audit = audit_result if isinstance(audit_result, dict) else {}
    check = check_result if isinstance(check_result, dict) else {}
    reasons: dict[str, list[str]] = {k: [] for k in _DRIFT_PRIORITY}

    manifest_errors: list[str] = []
    for source in (audit, check):
        manifest_errors.extend(e for e in _str_list(source.get("manifest_errors")) if e not in manifest_errors)
    # check_result dicts built without `manifest_errors` still carry the `harness.json ...` error strings.
    manifest_errors.extend(
        e for e in _str_list(check.get("errors")) if e.startswith(f"{HARNESS_JSON_NAME} ") and e not in manifest_errors
    )
    reasons["harness_gap"].extend(
        f"Malformed {HARNESS_DIR_NAME}/{VERIFY_MD_NAME if e.startswith(VERIFY_MD_NAME + ' ') else HARNESS_JSON_NAME}: {e}"
        for e in manifest_errors
    )

    severity = audit.get("severity")
    if severity == "missing" or (check and check.get("present") is False):
        reasons["doc_drift"].append(f"No {HARNESS_DIR_NAME}/{VERIFY_MD_NAME}: the harness is missing")
    for key, label in (
        ("uncovered_changed_files", "changed source files outside every feature_map glob"),
        ("stale_features", "features whose files changed since the baseline"),
        ("orphaned_globs", "feature_map globs matching no files"),
        ("new_routes", "new HTTP routes"),
        ("removed_routes", "removed HTTP routes"),
        ("new_cli_commands", "new CLI commands"),
        ("removed_cli_commands", "removed CLI commands"),
    ):
        items = _str_list(audit.get(key))
        if items:
            reasons["doc_drift"].append(f"{len(items)} {label}: " + ", ".join(items[:5]))
    for key, label in (
        ("stale_invariants", "formal invariants whose covered files changed since their sync_commit"),
        ("dead_cover_globs", "formal-invariant covers globs matching no files"),
        ("visual_baseline_changes", "frozen visual_diff baselines modified"),
    ):
        items = [i for i in audit.get(key) or [] if isinstance(i, dict)]
        if items:
            names = sorted({str(i.get("name") or i.get("hook")) for i in items})
            reasons["doc_drift"].append(f"{len(items)} {label}: " + ", ".join(names[:5]))
    gate_findings: list[str] = []
    for source in (audit, check):
        gate = _as_dict(source.get("gate_loosening"))
        if gate.get("loosened"):
            gate_findings.extend(f for f in _str_list(gate.get("findings")) if f not in gate_findings)
    reasons["doc_drift"].extend(gate_findings)

    doctor = _as_dict(check.get("doctor"))
    for r in doctor.get("results") or []:
        if isinstance(r, dict) and not r.get("ok"):
            why = "timed out" if r.get("timed_out") else f"exit {r.get('exit_code')}"
            reasons["harness_gap"].append(f"Doctor command `{_fmt_cmd(r.get('command'))}` failed ({why})")
    hook_results = _as_dict(check.get("hook_results"))
    for r in hook_results.get("results") or []:
        if not isinstance(r, dict) or r.get("ok"):
            continue
        name = r.get("name")
        kind = "optional" if r.get("optional") else "required"
        if r.get("timed_out") or r.get("exit_code") in _ENV_EXIT_CODES or r.get("exit_code") is None:
            why = "timed out" if r.get("timed_out") else f"could not run (exit {r.get('exit_code')})"
            reasons["harness_gap"].append(f"{kind.capitalize()} hook {name!r} {why}")
        elif kind == "required":
            reasons["product_regression"].append(f"Required hook {name!r} failed (exit {r.get('exit_code')})")
        # An optional hook that ran and failed is a disclosed limit, not a gate: it does not classify drift.
    runtime_prefixes = ("Doctor command failed:", "Required hook ")
    for err in _str_list(check.get("errors")):
        if err.startswith(runtime_prefixes) or err in gate_findings or err in manifest_errors:
            continue
        if err not in reasons["doc_drift"]:
            reasons["doc_drift"].append(err)

    classifications = [k for k in _DRIFT_PRIORITY if reasons[k]]
    classification = classifications[0] if classifications else "clean"
    return {
        "classification": classification,
        "classifications": classifications,
        "reasons": [f"{k}: {msg}" for k in _DRIFT_PRIORITY for msg in reasons[k]],
        "recommended_action": _DRIFT_ACTIONS[classification],
    }
