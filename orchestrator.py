"""Phase-based workstream orchestration, holdout isolation, and Kanban DAG creation."""

from __future__ import annotations

import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from .config import (
    PLUGIN_ID,
    WORKER_PROFILE,
    get_repo,
    get_root_hermes_home_path,
    load_settings,
    resolve_design_provider,
    resolve_phase_model,
)
from .harness import HARNESS_DIR_NAME, HOOK_KINDS, REQUIRED_SECTIONS, VERIFY_MD_NAME, discover_harness
from .metrics import thread_cards
from .trail import record_transition

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


def design_dir_path(tenant: str) -> Path:
    path = run_data_dir(tenant) / "design"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _has_design_artifacts(tenant: str) -> Optional[Path]:
    """Return the run's design/ directory if Phase 1b produced files there, else None."""
    d = run_data_dir(tenant) / "design"
    if d.is_dir():
        try:
            if any(p.name != ".gitkeep" for p in d.iterdir()):
                return d
        except OSError:
            pass
    return None


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


def _resolve_calling_session_id(explicit: Optional[str] = None) -> Optional[str]:
    """Resolve the active chat session id from explicit kwargs or Hermes session context."""
    if explicit and str(explicit).strip():
        return str(explicit).strip()
    try:
        from gateway.session_context import get_session_env

        for key in ("HERMES_SESSION_CHAT_ID", "HERMES_SESSION_KEY"):
            val = (get_session_env(key, "") or "").strip()
            if val:
                return val
    except Exception:
        pass
    return None


def _auto_subscribe_task(conn: Any, task_id: str, *, session_id: Optional[str] = None) -> bool:
    """Subscribe the calling chat session to completion/block wakes (matches `kanban_create`)."""
    try:
        from tools.kanban_tools import _maybe_auto_subscribe

        if _maybe_auto_subscribe(conn, task_id):
            return True
    except Exception:
        pass

    # Fallback when called with an explicit session_id outside gateway ContextVars (e.g. API/tests).
    if not session_id:
        return False
    try:
        from hermes_cli import kanban_db_notify as kbn

        chat_id = str(session_id).strip()
        if not chat_id:
            return False
        if any(
            sub["platform"] == "local" and sub["chat_id"] == chat_id
            for sub in kbn.list_notify_subs(conn, task_id)
        ):
            return True
        kbn.add_notify_sub(
            conn,
            task_id=task_id,
            platform="local",
            chat_id=chat_id,
            notifier_profile="default",
            delivery_mode="notify+wake",
        )
        return True
    except Exception:
        return False


def verification_gap_section(escaped_from: str) -> str:
    """Required Definition-card section for regressions that escaped a passed Verification."""
    return (
        f"## Verification Gap Analysis (required)\n"
        f"This regression escaped Verification card `{escaped_from}`. Before the task DAG, `spec.md` MUST include a "
        f"`## Verification Gap Analysis` section that analyzes BOTH independent verification lanes "
        f"(naming a single gate, or CI alone, is not sufficient):\n"
        f"1. Why Diff Check missed the bug: the code-level check, edge case, or blast-radius invariant that "
        f"failed to catch it.\n"
        f"2. Why Black-Box Check missed the bug: the observable behavior, acceptance criterion, or hidden scenario "
        f"gap that failed to catch it.\n"
        f"3. Proposes the cheapest durable structural prevention using the `/correct` enforcement ladder "
        f"Architecture -> Types -> Lint/CI check naming the fix -> Behavioral test -> Skill text last.\n"
        f"4. Proves the proposed check fails against the original escaped failure before the fix (and passes after), "
        f"recording the exact command, and adds a new hidden black-box scenario reproducing the escape "
        f"(never referenced from `spec.md`).\n"
    )


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
        if escaped_from:
            card_body += "\n" + verification_gap_section(str(escaped_from))
        skill_name = "factonio:define"
        goal_mode = True
        workspace_kind = "dir"
        workspace_path = repo_path
        contract = "local-only"

    effective_session_id = _resolve_calling_session_id(session_id)
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
            session_id=effective_session_id,
            board=board,
            completion_contract=contract,
        )
        subscribed = _auto_subscribe_task(conn, task_id, session_id=effective_session_id)

    record_transition(
        "start_workstream",
        tenant=tenant,
        repo_slug=slug,
        phase=phase,
        card_id=task_id,
        why=short_title,
        evidence={
            "kind": canon_kind,
            "skill": skill_name,
            "issue_number": issue_number,
            "skip_define": bool(go_direct),
            "escaped_from": escaped_from,
            "completion_contract": contract,
        },
        result="spawned",
    )
    if escaped_from:
        record_transition(
            "escaped_regression",
            tenant=tenant,
            repo_slug=slug,
            phase="escape",
            card_id=task_id,
            why=f"Regression escaped verification card {escaped_from}",
            evidence={"escaped_from": str(escaped_from), "fix_card_id": task_id},
            result="recorded",
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
        "subscribed": subscribed,
    }


def spawn_design_card(
    repo_spec: str,
    tenant: str,
    *,
    title: Optional[str] = None,
    brief: Optional[str] = None,
    provider: Optional[str] = None,
    parents: Optional[list[str]] = None,
    session_id: Optional[str] = None,
    board: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> dict[str, Any]:
    """Create a Phase 1b Design card (interactive Claude Design `/design` or pure-Hermes fallback)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing

    repo = get_repo(repo_spec)
    if repo is None:
        raise ValueError(f"Repository {repo_spec!r} is not registered with Factonio.")

    slug = str(repo["slug"])
    full_repo = str(repo["repo"])
    repo_path = str(repo["path"])
    spec_path = spec_file_path(tenant)
    design_dir = design_dir_path(tenant)
    design_info = resolve_design_provider(repo_slug=slug, override=provider)

    if design_info["effective"] == "off":
        record_transition(
            "skip_design",
            tenant=tenant,
            repo_slug=slug,
            phase="design",
            why=str(design_info["reason"]),
            evidence={"design_provider": design_info["configured"]},
            result="skipped",
        )
        return {
            "ok": False,
            "skipped": True,
            "phase": "design",
            "tenant": tenant,
            "repo": full_repo,
            "design_provider": design_info,
            "reason": design_info["reason"],
        }

    if design_info["configured"] == "claude-design" and not design_info["claude_available"]:
        raise ValueError(
            f"{design_info['reason']} Install/authenticate `claude`, or set `design_provider` to `auto` or `hermes`."
        )

    model_info = _phase_model_kwargs("design", slug)
    short_title = (title or f"UI/UX design for {tenant}").strip()[:108]
    brief_block = f"\n## Design Brief\n{brief.strip()}\n" if (brief and brief.strip()) else ""

    body = (
        f"## Factonio Design Card (Phase 1b)\n"
        f"- Repository: `{full_repo}` (`{repo_path}`)\n"
        f"- Workstream tenant: `{tenant}`\n"
        f"- Spec: `{spec_path}`\n"
        f"- Design output directory: `{design_dir}`\n"
        f"- Design provider: `{design_info['effective']}` (configured: `{design_info['configured']}`)\n"
        f"- Approval policy: `{design_info['approval']}`\n"
        f"{brief_block}\n"
        f"## Information Barrier\n"
        f"Read only the repository and `{spec_path}`. Do not search for or read any `holdout/` files.\n\n"
        f"## Instructions\n"
        f"Follow the preloaded `factonio:design` skill using provider `{design_info['effective']}`. "
        f"Write `{design_dir}/DESIGN.md` plus self-contained static HTML previews and rendered screenshots under `{design_dir}` "
        f"(and record the `claude.ai/artifact/<id>` link when using `claude-design`). "
        f"Do not edit repository production code or open a PR from this card.\n"
    )

    effective_session_id = _resolve_calling_session_id(session_id)
    with connect_closing(board=board) as conn:
        task_id = kb.create_task(
            conn,
            title=f"[design] {short_title}"[:120],
            body=body,
            assignee=WORKER_PROFILE,
            created_by=PLUGIN_ID,
            workspace_kind="dir",
            workspace_path=repo_path,
            project_id="",
            tenant=tenant,
            parents=parents or [],
            idempotency_key=idempotency_key,
            skills=["factonio:design"],
            model_override=model_info["model_override"],
            provider_override=model_info["provider_override"],
            goal_mode=True,
            session_id=effective_session_id,
            board=board,
            completion_contract="local-only",
        )
        subscribed = _auto_subscribe_task(conn, task_id, session_id=effective_session_id)

    record_transition(
        "spawn_design",
        tenant=tenant,
        repo_slug=slug,
        phase="design",
        card_id=task_id,
        why=short_title,
        evidence={
            "design_provider": design_info["effective"],
            "approval": design_info["approval"],
            "parents": list(parents or []),
        },
        result="spawned",
    )

    return {
        "ok": True,
        "task_id": task_id,
        "phase": "design",
        "skill": "factonio:design",
        "tenant": tenant,
        "repo": full_repo,
        "design_dir": str(design_dir),
        "design_provider": design_info,
        "model": model_info["resolved"],
        "subscribed": subscribed,
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
    base_branch: Optional[str] = None,
    stack_parent_pr: Optional[int] = None,
) -> dict[str, Any]:
    """Create one scoped Implementation worktree card (never exposes holdout/).

    ``base_branch`` (default: the repo's default branch) and ``stack_parent_pr`` make it a stacked
    card: a local branch is pre-seeded at the base tip when resolvable so the dispatcher's worktree
    starts on the parent, and the body instructs the worker to open the PR with ``--base``.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing

    from .repos import _validate_default_branch, _validate_pr_number, seed_stacked_branch

    if base_branch is not None:
        base_branch = _validate_default_branch(base_branch)
    if stack_parent_pr is not None:
        stack_parent_pr = _validate_pr_number(stack_parent_pr, "stack_parent_pr")

    repo = get_repo(repo_spec)
    if repo is None:
        raise ValueError(f"Repository {repo_spec!r} is not registered with Factonio.")

    slug = str(repo["slug"])
    full_repo = str(repo["repo"])
    repo_path = str(repo["path"])
    project_slug = str(repo.get("project_slug") or slug)
    spec_path = spec_file_path(tenant)
    design_dir = _has_design_artifacts(tenant)
    model_info = _phase_model_kwargs("implement", slug)
    contract = _completion_contract_for_repo(repo)
    design_line = f"- Approved Design directory: `{design_dir}` (treat `DESIGN.md` and previews as binding UI acceptance)\n" if design_dir else ""
    design_barrier_note = f" and `{design_dir}`" if design_dir else ""
    default_branch = str(repo.get("default_branch") or "main")
    target_base = base_branch or default_branch
    is_stacked = bool(stack_parent_pr is not None or target_base != default_branch)

    stack_lines = ""
    stack_section = ""
    if is_stacked:
        stack_lines = f"- Target base branch: `{target_base}`\n"
        if stack_parent_pr is not None:
            stack_lines += f"- Stack parent PR: `{full_repo}#{stack_parent_pr}`\n"
        stack_section = (
            f"\n## Stacked PR\n"
            f"This unit stacks on `{target_base}`, not `{default_branch}`. Run `git fetch origin {target_base}`, "
            f"rebase onto and diff against `origin/{target_base}...HEAD`, and open the PR with "
            f"`gh pr create --base {target_base}`. After the parent lands, the orchestrator restacks this PR "
            f"with `factonio_restack_pr`.\n"
        )

    body = (
        f"## Factonio Implementation Card\n"
        f"- Repository: `{full_repo}` (`{repo_path}`)\n"
        f"- Workstream tenant: `{tenant}`\n"
        f"- Spec: `{spec_path}`\n"
        f"{design_line}"
        f"- Default branch: `{repo.get('default_branch', 'main')}`\n"
        f"{stack_lines}"
        f"- Max fix rounds: `{repo.get('max_fix_rounds', 3)}`\n\n"
        f"## Work Unit\n{unit_body.strip()}\n"
        f"{stack_section}\n"
        f"## Information Barrier\n"
        f"Read only the repository, `{spec_path}`{design_barrier_note}. Do not search for or read any `holdout/` files.\n"
    )

    seed: Optional[dict[str, Any]] = None
    branch_kwargs: dict[str, Any] = {}
    if is_stacked and target_base != default_branch:
        candidate = f"{project_slug}/stack-{uuid.uuid4().hex[:8]}"
        seed = seed_stacked_branch(repo_path, target_base, candidate)
        if seed.get("ok"):
            branch_kwargs["branch_name"] = candidate
    branch_seeded = bool(branch_kwargs)

    effective_session_id = _resolve_calling_session_id(session_id)
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
            session_id=effective_session_id,
            board=board,
            completion_contract=contract,
            **branch_kwargs,
        )
        subscribed = _auto_subscribe_task(conn, task_id, session_id=effective_session_id)

    record_transition(
        "spawn_implement",
        tenant=tenant,
        repo_slug=slug,
        phase="implement",
        card_id=task_id,
        why=unit_title.strip(),
        evidence={
            "parents": list(parents or []),
            "design_linked": bool(design_dir),
            "completion_contract": contract,
            "base_branch": target_base,
            "is_stacked": is_stacked,
            "stack_parent_pr": stack_parent_pr,
            "branch_seeded": branch_seeded,
            "branch_name": branch_kwargs.get("branch_name"),
            "seed_reason": (seed or {}).get("reason"),
        },
        result="spawned",
    )

    return {
        "ok": True,
        "task_id": task_id,
        "phase": "implement",
        "tenant": tenant,
        "repo": full_repo,
        "model": model_info["resolved"],
        "completion_contract": contract,
        "subscribed": subscribed,
        "base_branch": target_base,
        "is_stacked": is_stacked,
        "stack_parent_pr": stack_parent_pr,
        "branch_seeded": branch_seeded,
        "branch_name": branch_kwargs.get("branch_name"),
    }


def _format_harness_section(repo_path: str | Path) -> str:
    """Black-Box-only card section pointing at the repo's `.factonio/` harness ("" when absent)."""
    try:
        info = discover_harness(repo_path)
    except Exception:
        return ""
    if not info.get("present"):
        return ""
    stages = " -> ".join(REQUIRED_SECTIONS)
    lines = [
        "## Repo Verification Harness",
        f"- Playbook: `{info['verify_md']}` (`.factonio/verify.md`)",
    ]
    if info.get("harness_json"):
        lines.append(f"- Manifest: `{info['harness_json']}` (`.factonio/harness.json`)")
    hooks = [h for h in info.get("hooks") or [] if isinstance(h, dict) and h.get("name")]
    if hooks:
        rendered = ", ".join(
            f"`{h['name']}` ({h.get('kind', 'custom')}, {'optional' if h.get('optional') else 'required'})"
            for h in hooks
        )
        lines.append(f"- Declared hooks: {rendered}")
    lines += [
        "",
        f"Read `.factonio/verify.md` (and `.factonio/harness.json` when present) from your fresh checkout of the PR head "
        f"(fall back to the paths above if the PR does not carry them). Execute its stages in order: {stages}. "
        f"Run every declared hook ({', '.join(HOOK_KINDS)}) or `hermes factonio harness check <repo> --run-doctor "
        f"--run-hooks --json` from the checkout: a failing required hook is a `reject`, a failing optional hook is a "
        f"`disclosed_limits` entry. The harness is a black-box playbook, not implementation source: it does not lift "
        f"the ban on `git diff` or reading source files.",
    ]
    return "\n".join(lines) + "\n"


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
    design_dir = _has_design_artifacts(tenant)
    parents = [impl_card_id] if impl_card_id else []
    sha_line = f"- Expected HEAD SHA: `{head_sha}`\n" if head_sha else ""
    design_line = f"- Approved Design directory: `{design_dir}`\n" if design_dir else ""
    pr_ref = f"{full_repo}#{int(pr_number)}"

    diff_model = _phase_model_kwargs("verify", slug, subphase="verify_diff")
    bb_model = _phase_model_kwargs("verify", slug, subphase="verify_blackbox")

    # Diff Check: sees spec + approved design (if any) + PR diff; NEVER given holdout_dir.
    diff_body = (
        f"## Factonio Diff Verification Card\n"
        f"- Repository: `{full_repo}` (`{repo_path}`)\n"
        f"- Pull Request: `{pr_ref}`\n"
        f"{sha_line}"
        f"- Workstream tenant: `{tenant}`\n"
        f"- Spec: `{spec_path}`\n"
        f"{design_line}\n"
        f"## Information Barrier\n"
        f"Audit the PR diff (`gh pr diff {int(pr_number)} --repo {full_repo}`) against `{spec_path}`"
        f"{f' and `{design_dir}`' if design_dir else ''}. "
        f"Do not inspect any `holdout/` directory. Return structured metadata with `verdict: pass|reject`, "
        f"`pr_number: {int(pr_number)}`, `repo: {full_repo}`, and `findings: [...]`.\n"
    )

    # Black-Box Check: sees acceptance criteria + approved design previews (if any) + holdout + built app;
    # FORBIDDEN from reading git diff or source files.
    hint_section = f"\n## Run / Target Hint\n{run_hint.strip()}\n" if run_hint else ""
    harness_section = _format_harness_section(repo_path)
    harness_path = str(Path(repo_path) / HARNESS_DIR_NAME / VERIFY_MD_NAME) if harness_section else None
    if harness_section:
        hint_section += f"\n{harness_section}"
    bb_body = (
        f"## Factonio Black-Box Verification Card\n"
        f"- Repository: `{full_repo}` (`{repo_path}`)\n"
        f"- Pull Request: `{pr_ref}`\n"
        f"{sha_line}"
        f"- Workstream tenant: `{tenant}`\n"
        f"- Acceptance spec: `{spec_path}` (read ONLY the Goals & Acceptance Criteria sections)\n"
        f"{design_line}"
        f"- Hidden holdout directory: `{holdout_dir}`\n"
        f"{hint_section}\n"
        f"## Information Barrier\n"
        f"Verify observable behavior only by building/running the PR branch in a fresh checkout or test harness "
        f"and executing the acceptance criteria plus every scenario under `{holdout_dir}`"
        f"{f', comparing rendered UI fidelity against `{design_dir}`' if design_dir else ''}. "
        f"NEVER run `git diff`, NEVER inspect PR diffs, and NEVER read implementation source code. "
        f"Return structured metadata with `verdict: pass|reject|inconclusive`, `pr_number: {int(pr_number)}`, "
        f"`repo: {full_repo}`, `scenarios_run`, and `findings: [...]`.\n"
    )

    effective_session_id = _resolve_calling_session_id(session_id)
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
            session_id=effective_session_id,
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
            session_id=effective_session_id,
            board=board,
            completion_contract="local-only",
        )
        diff_sub = _auto_subscribe_task(conn, diff_id, session_id=effective_session_id)
        bb_sub = _auto_subscribe_task(conn, bb_id, session_id=effective_session_id)

    record_transition(
        "spawn_verify",
        tenant=tenant,
        repo_slug=slug,
        phase="verify",
        card_id=impl_card_id or "",
        why=f"PR #{int(pr_number)} opened" + (f" at {head_sha[:12]}" if head_sha else ""),
        evidence={
            "pr_number": int(pr_number),
            "head_sha": head_sha,
            "verify_diff_task_id": diff_id,
            "verify_blackbox_task_id": bb_id,
            "harness_present": bool(harness_section),
        },
        result="spawned",
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
        "subscribed": bool(diff_sub and bb_sub),
        "harness_present": bool(harness_section),
        "harness_path": harness_path,
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
