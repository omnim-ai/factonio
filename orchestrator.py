"""Phase-based workstream orchestration, holdout isolation, and Kanban DAG creation."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Optional

from .config import (
    PLUGIN_ID,
    WORKER_PROFILE,
    get_repo,
    get_root_hermes_home_path,
    load_settings,
    resolve_phase_model,
)
from .metrics import thread_cards

_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def make_tenant(repo_slug: str, *, issue_number: Optional[int] = None, label: Optional[str] = None) -> str:
    """Generate a clean kebab-case Kanban tenant slug for a workstream."""
    base = _SLUG_RE.sub("-", repo_slug.lower()).strip("-")
    if issue_number is not None:
        return f"{base}-i{int(issue_number)}"[:64]
    if label:
        tail = _SLUG_RE.sub("-", label.lower()).strip("-")[:24]
        if tail:
            return f"{base}-{tail}"[:64]
    return f"{base}-{int(time.time()) % 100000}"[:64]


def run_data_dir(tenant: str) -> Path:
    """Durable plugin-owned run directory holding `spec.md` and `holdout/`."""
    safe = _SLUG_RE.sub("-", (tenant or "default").lower()).strip("-") or "default"
    path = get_root_hermes_home_path() / "plugin-data" / PLUGIN_ID / "runs" / safe
    path.mkdir(parents=True, exist_ok=True)
    return path


def spec_file_path(tenant: str) -> Path:
    return run_data_dir(tenant) / "spec.md"


def holdout_dir_path(tenant: str) -> Path:
    path = run_data_dir(tenant) / "holdout"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _phase_model_kwargs(phase: str, repo_slug: str, subphase: Optional[str] = None) -> dict[str, Any]:
    resolved = resolve_phase_model(phase, repo_slug=repo_slug, subphase=subphase)
    model = resolved.get("model")
    provider = resolved.get("provider")
    if not model:
        return {"model_override": None, "provider_override": None, "resolved": resolved}
    return {
        "model_override": model,
        "provider_override": provider or None,
        "resolved": resolved,
    }


def _completion_contract_for_repo(repo: dict[str, Any]) -> str:
    """Use native GitHub PR contract when branch protection defines required checks."""
    ci_mode = str(repo.get("ci_contract") or "auto").lower()
    if ci_mode == "local-only":
        return "local-only"
    if ci_mode == "github" or (ci_mode == "auto" and repo.get("has_required_checks")):
        return str(repo["repo"])
    return "local-only"


def start_workstream(
    repo_spec: str,
    request: str,
    *,
    kind: str = "feature",
    title: Optional[str] = None,
    issue_number: Optional[int] = None,
    skip_define: bool = False,
    escaped_from: Optional[str] = None,
    session_id: Optional[str] = None,
    board: Optional[str] = None,
) -> dict[str, Any]:
    """Start a Factonio workstream on a registered GitHub repository."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing

    repo = get_repo(repo_spec)
    if repo is None:
        raise ValueError(
            f"Repository {repo_spec!r} is not registered with Factonio. "
            "Run `hermes factonio add owner/repo` or call `factonio_add_repo` first."
        )

    canon_kind = (kind or "feature").strip().lower()
    if canon_kind not in {"feature", "bug", "quick"}:
        raise ValueError(f"kind must be 'feature', 'bug', or 'quick', got {kind!r}")

    slug = str(repo["slug"])
    full_repo = str(repo["repo"])
    repo_path = str(repo["path"])
    project_slug = str(repo.get("project_slug") or slug)
    short_title = (title or request.strip().splitlines()[0])[:72].strip()
    tenant = make_tenant(slug, issue_number=issue_number, label=short_title if issue_number is None else None)
    spec_path = spec_file_path(tenant)
    holdout_dir = holdout_dir_path(tenant)

    go_direct = skip_define or canon_kind == "quick"
    phase = "implement" if go_direct else "define"
    model_info = _phase_model_kwargs(phase, slug)

    escape_prefix = "[escape] " if escaped_from else ""
    escape_line = f"\nEscaped from: {escaped_from}\n" if escaped_from else ""
    issue_line = f"- GitHub Issue: {full_repo}#{int(issue_number)}\n" if issue_number is not None else ""

    if go_direct:
        card_title = f"{escape_prefix}[implement] {short_title}"
        card_body = (
            f"## Factonio Implementation Card\n"
            f"- Repository: `{full_repo}` (`{repo_path}`)\n"
            f"- Workstream tenant: `{tenant}`\n"
            f"- Merge policy: `{repo.get('merge_policy', 'auto')}`\n"
            f"- Max fix rounds: `{repo.get('max_fix_rounds', 3)}`\n"
            f"{issue_line}{escape_line}\n"
            f"## Task\n{request.strip()}\n\n"
            f"## Instructions\n"
            f"Follow the preloaded `factonio:implement` skill. Implement the scoped change in this worktree, "
            f"run repository tests, push the branch, open a PR against `{repo.get('default_branch', 'main')}`, "
            f"and record `pr_number`, `published_pr`, `head_sha`, and `repo` in completion metadata.\n"
        )
        skill_name = "factonio:implement"
        goal_mode = False
        workspace_kind = "worktree"
        workspace_path = repo_path
        contract = _completion_contract_for_repo(repo)
    else:
        mode_label = "bug diagnosis" if canon_kind == "bug" else "feature definition"
        card_title = f"{escape_prefix}[define] {short_title}"
        card_body = (
            f"## Factonio Definition Card ({mode_label})\n"
            f"- Repository: `{full_repo}` (`{repo_path}`)\n"
            f"- Workstream tenant: `{tenant}`\n"
            f"- Mode: `{canon_kind}`\n"
            f"- Spec output path: `{spec_path}`\n"
            f"- Hidden holdout directory: `{holdout_dir}`\n"
            f"{issue_line}{escape_line}\n"
            f"## Request\n{request.strip()}\n\n"
            f"## Instructions\n"
            f"Follow the preloaded `factonio:define` skill. Inspect the repository at `{repo_path}`, "
            f"write `{spec_path}` (goals, acceptance criteria, technical approach, and parallel task DAG), "
            f"and write hidden black-box verification scenarios under `{holdout_dir}` (never referenced inside `spec.md`).\n"
        )
        skill_name = "factonio:define"
        goal_mode = True
        workspace_kind = "dir"
        workspace_path = repo_path
        contract = "local-only"

    with connect_closing(board=board) as conn:
        task_id = kb.create_task(
            conn,
            title=card_title,
            body=card_body,
            assignee=WORKER_PROFILE,
            created_by=PLUGIN_ID,
            workspace_kind=workspace_kind,
            workspace_path=workspace_path,
            project_id=project_slug if go_direct else "",
            tenant=tenant,
            skills=[skill_name],
            model_override=model_info["model_override"],
            provider_override=model_info["provider_override"],
            goal_mode=goal_mode,
            session_id=session_id,
            board=board,
            completion_contract=contract,
        )

    return {
        "ok": True,
        "task_id": task_id,
        "phase": phase,
        "skill": skill_name,
        "tenant": tenant,
        "repo": full_repo,
        "repo_slug": slug,
        "spec_path": str(spec_path),
        "holdout_dir": str(holdout_dir),
        "model": model_info["resolved"],
        "completion_contract": contract,
    }


def spawn_implementation_card(
    repo_spec: str,
    tenant: str,
    unit_title: str,
    unit_body: str,
    *,
    parents: Optional[list[str]] = None,
    session_id: Optional[str] = None,
    board: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> dict[str, Any]:
    """Create one scoped Implementation worktree card (never exposes holdout/)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing

    repo = get_repo(repo_spec)
    if repo is None:
        raise ValueError(f"Repository {repo_spec!r} is not registered with Factonio.")

    slug = str(repo["slug"])
    full_repo = str(repo["repo"])
    repo_path = str(repo["path"])
    project_slug = str(repo.get("project_slug") or slug)
    spec_path = spec_file_path(tenant)
    model_info = _phase_model_kwargs("implement", slug)
    contract = _completion_contract_for_repo(repo)

    body = (
        f"## Factonio Implementation Card\n"
        f"- Repository: `{full_repo}` (`{repo_path}`)\n"
        f"- Workstream tenant: `{tenant}`\n"
        f"- Spec: `{spec_path}`\n"
        f"- Default branch: `{repo.get('default_branch', 'main')}`\n"
        f"- Max fix rounds: `{repo.get('max_fix_rounds', 3)}`\n\n"
        f"## Work Unit\n{unit_body.strip()}\n\n"
        f"## Information Barrier\n"
        f"Read only the repository and `{spec_path}`. Do not search for or read any `holdout/` files.\n"
    )

    with connect_closing(board=board) as conn:
        task_id = kb.create_task(
            conn,
            title=f"[implement] {unit_title.strip()}"[:120],
            body=body,
            assignee=WORKER_PROFILE,
            created_by=PLUGIN_ID,
            workspace_kind="worktree",
            workspace_path=repo_path,
            project_id=project_slug,
            tenant=tenant,
            parents=parents or [],
            idempotency_key=idempotency_key,
            skills=["factonio:implement"],
            model_override=model_info["model_override"],
            provider_override=model_info["provider_override"],
            goal_mode=False,
            session_id=session_id,
            board=board,
            completion_contract=contract,
        )

    return {
        "ok": True,
        "task_id": task_id,
        "phase": "implement",
        "tenant": tenant,
        "repo": full_repo,
        "model": model_info["resolved"],
        "completion_contract": contract,
    }


def spawn_verification_pair(
    repo_spec: str,
    tenant: str,
    pr_number: int,
    *,
    impl_card_id: Optional[str] = None,
    head_sha: Optional[str] = None,
    run_hint: Optional[str] = None,
    session_id: Optional[str] = None,
    board: Optional[str] = None,
) -> dict[str, Any]:
    """Spawn two isolated parallel Verification cards (Diff Check and Black-Box Check)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing

    repo = get_repo(repo_spec)
    if repo is None:
        raise ValueError(f"Repository {repo_spec!r} is not registered with Factonio.")

    slug = str(repo["slug"])
    full_repo = str(repo["repo"])
    repo_path = str(repo["path"])
    spec_path = spec_file_path(tenant)
    holdout_dir = holdout_dir_path(tenant)
    parents = [impl_card_id] if impl_card_id else []
    sha_line = f"- Expected HEAD SHA: `{head_sha}`\n" if head_sha else ""
    pr_ref = f"{full_repo}#{int(pr_number)}"

    diff_model = _phase_model_kwargs("verify", slug, subphase="verify_diff")
    bb_model = _phase_model_kwargs("verify", slug, subphase="verify_blackbox")

    # Diff Check: sees spec + PR diff; NEVER given holdout_dir.
    diff_body = (
        f"## Factonio Diff Verification Card\n"
        f"- Repository: `{full_repo}` (`{repo_path}`)\n"
        f"- Pull Request: `{pr_ref}`\n"
        f"{sha_line}"
        f"- Workstream tenant: `{tenant}`\n"
        f"- Spec: `{spec_path}`\n\n"
        f"## Information Barrier\n"
        f"Audit the PR diff (`gh pr diff {int(pr_number)} --repo {full_repo}`) against `{spec_path}`. "
        f"Do not inspect any `holdout/` directory. Return structured metadata with `verdict: pass|reject`, "
        f"`pr_number: {int(pr_number)}`, `repo: {full_repo}`, and `findings: [...]`.\n"
    )

    # Black-Box Check: sees acceptance criteria + holdout + built app; FORBIDDEN from reading git diff or source files.
    hint_section = f"\n## Run / Target Hint\n{run_hint.strip()}\n" if run_hint else ""
    bb_body = (
        f"## Factonio Black-Box Verification Card\n"
        f"- Repository: `{full_repo}` (`{repo_path}`)\n"
        f"- Pull Request: `{pr_ref}`\n"
        f"{sha_line}"
        f"- Workstream tenant: `{tenant}`\n"
        f"- Acceptance spec: `{spec_path}` (read ONLY the Goals & Acceptance Criteria sections)\n"
        f"- Hidden holdout directory: `{holdout_dir}`\n"
        f"{hint_section}\n"
        f"## Information Barrier\n"
        f"Verify observable behavior only by building/running the PR branch in a fresh checkout or test harness "
        f"and executing the acceptance criteria plus every scenario under `{holdout_dir}`. "
        f"NEVER run `git diff`, NEVER inspect PR diffs, and NEVER read implementation source code. "
        f"Return structured metadata with `verdict: pass|reject`, `pr_number: {int(pr_number)}`, "
        f"`repo: {full_repo}`, `scenarios_run`, and `findings: [...]`.\n"
    )

    with connect_closing(board=board) as conn:
        diff_id = kb.create_task(
            conn,
            title=f"[verify:diff] {pr_ref}",
            body=diff_body,
            assignee=WORKER_PROFILE,
            created_by=PLUGIN_ID,
            workspace_kind="scratch",
            project_id="",
            tenant=tenant,
            parents=parents,
            skills=["factonio:verify-diff"],
            model_override=diff_model["model_override"],
            provider_override=diff_model["provider_override"],
            goal_mode=False,
            session_id=session_id,
            board=board,
            completion_contract="local-only",
        )
        bb_id = kb.create_task(
            conn,
            title=f"[verify:blackbox] {pr_ref}",
            body=bb_body,
            assignee=WORKER_PROFILE,
            created_by=PLUGIN_ID,
            workspace_kind="scratch",
            project_id="",
            tenant=tenant,
            parents=parents,
            skills=["factonio:verify-blackbox"],
            model_override=bb_model["model_override"],
            provider_override=bb_model["provider_override"],
            goal_mode=False,
            session_id=session_id,
            board=board,
            completion_contract="local-only",
        )

    return {
        "ok": True,
        "tenant": tenant,
        "repo": full_repo,
        "pr_number": int(pr_number),
        "verify_diff_task_id": diff_id,
        "verify_blackbox_task_id": bb_id,
        "diff_model": diff_model["resolved"],
        "blackbox_model": bb_model["resolved"],
    }


def workstream_status(
    *,
    tenant: Optional[str] = None,
    session_id: Optional[str] = None,
    board: Optional[str] = None,
) -> dict[str, Any]:
    """Return current Kanban cards and phase counts for a Factonio workstream."""
    try:
        from hermes_cli.kanban_db import kanban_db_path

        db_path = Path(kanban_db_path(board=board))
    except Exception:
        db_path = get_root_hermes_home_path() / "kanban.db"

    data = thread_cards(db_path, session_id=session_id, tenant=tenant)
    return {
        "ok": True,
        "tenant": tenant,
        "settings_merge_policy": load_settings(use_root=True).get("default_merge_policy", "auto"),
        **data,
    }
