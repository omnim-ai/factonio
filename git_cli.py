"""Layer 0 `git` / `gh` subprocess seam (stdlib only).

Every Factonio git/gh call funnels through `run_command`, which resolves its runner at call time:
an explicit ``runner=``, else the `use_runner` override, else ``subprocess.run``. Looking
``subprocess.run`` up per call keeps ``mock.patch("subprocess.run")`` test seams working, and
modules keep their own patchable wrappers (``factonio.repos._run_gh`` / ``_run_git``).
"""

from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Union

Runner = Callable[..., Any]
PathLike = Union[str, Path]

_RUNNER_OVERRIDE: Optional[Runner] = None


@contextlib.contextmanager
def use_runner(runner: Optional[Runner]) -> Iterator[None]:
    """Route every `run_command` call without an explicit ``runner`` through ``runner`` (tests)."""
    global _RUNNER_OVERRIDE
    prior = _RUNNER_OVERRIDE
    _RUNNER_OVERRIDE = runner
    try:
        yield
    finally:
        _RUNNER_OVERRIDE = prior


def run_command(
    argv: list[str],
    *,
    cwd: Optional[PathLike] = None,
    input_text: Optional[str] = None,
    timeout: float = 30,
    text: bool = True,
    capture_errors: bool = False,
    runner: Optional[Runner] = None,
) -> subprocess.CompletedProcess:
    """Run ``argv`` with captured output and ``check=False``.

    ``capture_errors`` turns a spawn failure or timeout into a ``returncode=128`` result whose
    ``stderr`` carries the error, for read-only probes that must never raise.
    """
    run = runner or _RUNNER_OVERRIDE or subprocess.run
    kwargs: dict[str, Any] = {"cwd": str(cwd) if cwd else None, "capture_output": True, "timeout": timeout, "check": False}
    if text:
        kwargs["text"] = True
    if input_text is not None:
        kwargs["input"] = input_text
    try:
        return run(list(argv), **kwargs)
    except (OSError, subprocess.SubprocessError) as exc:
        if not capture_errors:
            raise
        empty: Any = "" if text else b""
        err: Any = str(exc) if text else str(exc).encode("utf-8", "replace")
        return subprocess.CompletedProcess(list(argv), 128, empty, err)


def run_git(
    args: list[str],
    *,
    cwd: PathLike,
    input_text: Optional[str] = None,
    timeout: float = 30,
    text: bool = True,
    capture_errors: bool = False,
    runner: Optional[Runner] = None,
) -> subprocess.CompletedProcess:
    return run_command(
        ["git", *args], cwd=cwd, input_text=input_text, timeout=timeout, text=text,
        capture_errors=capture_errors, runner=runner,
    )


def run_git_ro(
    args: list[str],
    *,
    cwd: PathLike,
    timeout: float = 30,
    text: bool = True,
    capture_errors: bool = False,
    runner: Optional[Runner] = None,
) -> subprocess.CompletedProcess:
    """Read-only git probe: never refresh/rewrite the index or take optional locks."""
    return run_git(
        ["--no-optional-locks", *args], cwd=cwd, timeout=timeout, text=text,
        capture_errors=capture_errors, runner=runner,
    )


def run_gh(
    args: list[str],
    *,
    cwd: Optional[PathLike] = None,
    timeout: float = 30,
    runner: Optional[Runner] = None,
) -> subprocess.CompletedProcess:
    return run_command(["gh", *args], cwd=cwd, timeout=timeout, runner=runner)


def is_git_repo(path: PathLike, *, runner: Optional[Runner] = None) -> bool:
    proc = run_git_ro(["rev-parse", "--is-inside-work-tree"], cwd=path, capture_errors=True, runner=runner)
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def git_err(proc: subprocess.CompletedProcess, what: str) -> str:
    detail = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
    return f"git {what} failed: {detail}"


def parse_status_z(text: str) -> list[tuple[str, str]]:
    """Parse ``git status --porcelain=v1 -z`` into ``(XY, path)`` pairs (rename sources skipped)."""
    out: list[tuple[str, str]] = []
    records = text.split("\0")
    i = 0
    while i < len(records):
        rec = records[i]
        i += 1
        if len(rec) < 4:
            continue
        code, path = rec[:2], rec[3:]
        out.append((code, path))
        if "R" in code or "C" in code:
            i += 1  # the next record is the rename/copy source path
    return out
