"""Opt-in `/arena` mode: competing Definition or Design candidates plus a cross-vendor judge.

`spawn_arena` fans one isolated candidate card out per member of a multi-vendor model panel
(reusing `config.resolve_verify_panel`), each writing only inside its own
`runs/<tenant>/arena/<arena_id>/candidates/<slot>/` directory, and a dependent judge card that
hand-grafts the canonical `spec.md` (+ `holdout/`) or `design/` from every candidate.
`evaluate_arena` reports progress from a read-only `kanban.db` connection plus on-disk artifacts.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

from .config import (
    PLUGIN_ID,
    WORKER_PROFILE,
    _normalize_panel_entry,
    get_repo,
    get_root_hermes_home_path,
    infer_model_vendor,
    resolve_phase_model,
    resolve_verify_panel,
    single_line,
)
from .orchestrator import (
    _SLUG_RE,
    _auto_subscribe_task,
    _kanban_db_file,
    _resolve_calling_session_id,
    _stamp_token,
    design_dir_path,
    holdout_dir_path,
    make_tenant,
    run_data_dir,
    spec_file_path,
)
from .trail import contains_holdout_reference, record_transition

ARENA_TARGETS: tuple[str, ...] = ("define", "design")
ARENA_ID_RE = re.compile(r"^ar_[0-9a-f]{6,32}$")
MANIFEST_NAME = "manifest.json"
_DONE = "done"
_STUCK = frozenset({"blocked", "triage"})
# Terminal non-done candidate states: cards gone from the board vs. cards that ended without output.
_GONE = frozenset({"missing", "archived"})
_FAILED = frozenset({"failed", "cancelled"})
_SLOT_SAFE_RE = re.compile(r"[^a-z0-9-]+")


def _runs_root() -> Path:
    return get_root_hermes_home_path() / "plugin-data" / PLUGIN_ID / "runs"


def _safe_tenant(tenant: str) -> str:
    return _SLUG_RE.sub("-", (tenant or "default").lower()).strip("-") or "default"


def _arena_dir(tenant: str, arena_id: str) -> Path:
    return run_data_dir(tenant) / "arena" / arena_id


def _stamp(arena_id: str, *, role: str, target: str, vendor: Any, model: Any, slot: Optional[str] = None) -> str:
    slot_part = f", slot=`{_stamp_token(slot, 'unknown')}`" if slot else ""
    return (
        f"- Arena: `{arena_id}` (role=`{role}`, target=`{target}`{slot_part}, "
        f"vendor=`{_stamp_token(vendor, 'unknown')}`, model=`{_stamp_token(model, 'unset')}`)\n"
    )


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"
    _write_bytes_atomic(path, text.encode("utf-8"))


def _diversity_error(panel: dict[str, Any]) -> str:
    entries = panel.get("entries") or []
    vendors = panel.get("vendors") or []
    labels = ", ".join(f"{e['label']} [{e.get('vendor') or 'unknown'}]" for e in entries) or "none"
    return (
        f"Arena requires >= 2 candidate models spanning >= 2 distinct known vendor families; got "
        f"{len(entries)} candidate(s) across {len(vendors)} known vendor famil{'y' if len(vendors) == 1 else 'ies'} "
        f"({labels}; source: {panel.get('source')}). Pass candidate_models such as "
        f"['anthropic/claude-opus-4-6', 'openai/gpt-5'] or configure a verify_panel. No cards were created."
    )


def _normalize_provider(provider: Any) -> Optional[str]:
    text = str(provider or "").strip()
    return None if not text or text.lower() == "auto" else text


def _resolve_judge(
    *,
    repo_slug: str,
    target: str,
    judge_model: Any,
    candidate_vendors: set[str],
    strict_cross_vendor: bool,
    has_unknown_candidate: bool = False,
    saved_judge: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Pick the judge model and enforce (``strict``) or advise on cross-vendor independence.

    Precedence: explicit ``judge_model`` > the ``saved_judge`` persisted in the arena manifest >
    the repo/aux ``<target>`` then ``verify`` phase models. Independence is only provable when every
    candidate vendor is known, so an unknown-vendor candidate makes ``cross_vendor`` false.
    """
    entry: Optional[dict[str, Any]] = None
    source = "unset"
    if judge_model not in (None, "", {}):
        entry = _normalize_panel_entry(judge_model, source="override", index=0)
        if entry is None:
            raise ValueError(f"judge_model must name a model ('provider/model', 'model', or {{model: ...}}); got {judge_model!r}")
        source = "override"
    elif saved_judge and str(saved_judge.get("model") or "").strip():
        entry = _normalize_panel_entry(
            {"provider": saved_judge.get("provider"), "model": saved_judge.get("model")}, source="manifest", index=0
        )
        source = "manifest"
    else:
        options: list[tuple[dict[str, Any], str]] = []
        for phase in (target, "verify"):
            resolved = resolve_phase_model(phase, repo_slug=repo_slug)
            if not resolved.get("model"):
                continue
            provider = _normalize_provider(resolved.get("provider"))
            model = str(resolved["model"])
            options.append((
                {
                    "provider": provider,
                    "model": model,
                    "label": f"{provider}/{model}" if provider else model,
                    "vendor": infer_model_vendor(provider, model),
                },
                str(resolved.get("source") or phase),
            ))
        distinct = [opt for opt in options if opt[0]["vendor"] and opt[0]["vendor"] not in candidate_vendors]
        chosen = (distinct or options or [None])[0]
        if chosen is not None:
            entry, source = chosen
    if entry is not None:
        provider = _normalize_provider(entry.get("provider"))
        entry = {**entry, "provider": provider, "label": f"{provider}/{entry['model']}" if provider else str(entry["model"])}

    vendor = (entry or {}).get("vendor")
    cross_vendor = bool(vendor) and vendor not in candidate_vendors and not has_unknown_candidate
    advisory: Optional[str] = None
    if not cross_vendor:
        if not entry:
            reason = "no judge model is configured (it would run on the worker profile default)"
        elif not vendor:
            reason = f"the vendor of judge model {entry['label']} could not be inferred"
        elif vendor in candidate_vendors:
            reason = f"judge model {entry['label']} shares vendor family {vendor} with a candidate"
        else:
            reason = "a candidate's vendor family could not be inferred, so judge independence is unprovable"
        if strict_cross_vendor:
            raise ValueError(
                f"strict_cross_vendor: {reason}; candidates span {', '.join(sorted(candidate_vendors))}. "
                "Pass a judge_model from a different vendor family. No cards were created."
            )
        advisory = f"Judge is not provably cross-vendor: {reason}."
    return {
        "provider": (entry or {}).get("provider"),
        "model": (entry or {}).get("model"),
        "label": (entry or {}).get("label"),
        "vendor": vendor,
        "source": source,
        "cross_vendor": cross_vendor,
        "advisory": advisory,
    }


def _slot_name(index: int, vendor: Optional[str]) -> str:
    tail = _SLOT_SAFE_RE.sub("-", str(vendor or "unknown").lower()).strip("-") or "unknown"
    return f"c{index}-{tail}"


def _candidate_body(
    *,
    target: str,
    arena_id: str,
    cand: dict[str, Any],
    full_repo: str,
    repo_path: str,
    tenant: str,
    arena_dir: Path,
    request: str,
    canonical_spec: Path,
) -> str:
    stamp = _stamp(arena_id, role="candidate", target=target, slot=cand["slot"], vendor=cand["vendor"], model=cand["label"])
    isolation = (
        f"## Arena Isolation\n"
        f"You are one independent candidate in an opt-in arena. Write ONLY inside `{cand['dir']}`. Do not read, "
        f"wait for, or coordinate with other candidates under `{arena_dir / 'candidates'}`, and do not write the "
        f"canonical artifact: a separate cross-vendor judge synthesizes every candidate after you finish. "
        f"Record `arena_id: {arena_id}` and `slot: {cand['slot']}` in completion metadata.\n\n"
    )
    if target == "define":
        return (
            f"## Factonio Arena Candidate Card (define)\n"
            f"- Repository: `{full_repo}` (`{repo_path}`)\n"
            f"- Workstream tenant: `{tenant}`\n"
            f"{stamp}"
            f"- Candidate spec output path: `{cand['spec_path']}`\n"
            f"- Candidate hidden holdout directory: `{cand['holdout_dir']}`\n\n"
            f"## Request\n{request}\n\n"
            f"{isolation}"
            f"## Instructions\n"
            f"Follow the preloaded `factonio:define` skill. Inspect the repository at `{repo_path}`, write "
            f"`{cand['spec_path']}` (goals, acceptance criteria, technical approach, and parallel task DAG), and write "
            f"hidden black-box verification scenarios under `{cand['holdout_dir']}` (never referenced inside the spec). "
            f"Do not write `{canonical_spec}`.\n"
        )
    return (
        f"## Factonio Arena Candidate Card (design)\n"
        f"- Repository: `{full_repo}` (`{repo_path}`)\n"
        f"- Workstream tenant: `{tenant}`\n"
        f"{stamp}"
        f"- Spec: `{canonical_spec}`\n"
        f"- Candidate design output directory: `{cand['design_dir']}`\n"
        f"- Design provider: `hermes` (pure-Hermes prototyping on this candidate's own model, so every vendor "
        f"designs independently)\n\n"
        f"## Design Brief\n{request}\n\n"
        f"## Information Barrier\n"
        f"Read only the repository and `{canonical_spec}`. Do not search for or read any `holdout/` files.\n\n"
        f"{isolation}"
        f"## Instructions\n"
        f"Follow the preloaded `factonio:design` skill using provider `hermes`. Write `{cand['design_dir']}/DESIGN.md` "
        f"plus self-contained static HTML previews and rendered screenshots under `{cand['design_dir']}`. "
        f"Do not edit repository production code or open a PR from this card.\n"
    )


def _judge_body(
    *,
    target: str,
    arena_id: str,
    judge: dict[str, Any],
    candidates: list[dict[str, Any]],
    full_repo: str,
    repo_path: str,
    tenant: str,
    manifest_path: Path,
    request: str,
    canonical_spec: Path,
    canonical_holdout: Optional[Path],
    canonical_design: Optional[Path],
) -> str:
    stamp = _stamp(arena_id, role="judge", target=target, vendor=judge.get("vendor"), model=judge.get("label"))
    lines = []
    for cand in candidates:
        if target == "define":
            artifacts = f"spec `{cand['spec_path']}`, holdout `{cand['holdout_dir']}`"
        else:
            artifacts = f"design `{cand['design_dir']}`"
        lines.append(
            f"- `{cand['slot']}` (vendor `{_stamp_token(cand['vendor'], 'unknown')}`, model "
            f"`{_stamp_token(cand['label'], 'unset')}`, card `{cand['task_id']}`): {artifacts}"
        )
    if target == "define":
        outputs = (
            f"- Canonical spec output path: `{canonical_spec}`\n"
            f"- Canonical hidden holdout directory: `{canonical_holdout}`\n"
        )
        canonical = f"`{canonical_spec}` (and the merged hidden scenarios under `{canonical_holdout}`, never referenced or quoted inside `spec.md`)"
        skill = "factonio:define"
        barrier = ""
        canonical_path = str(canonical_spec)
    else:
        outputs = (
            f"- Spec: `{canonical_spec}`\n- Canonical design output directory: `{canonical_design}`\n"
            f"- Design provider: `hermes` (pure-Hermes synthesis on this judge's own model)\n"
        )
        canonical = f"`{canonical_design}/DESIGN.md` plus the grafted static HTML previews and screenshots under `{canonical_design}`"
        skill = "factonio:design"
        barrier = (
            f"## Information Barrier\n"
            f"Read only the repository, `{canonical_spec}`, and the candidate design directories above. "
            f"Do not search for or read any `holdout/` files.\n\n"
        )
        canonical_path = str(canonical_design)
    return (
        f"## Factonio Arena Judge Card ({target} synthesis)\n"
        f"- Repository: `{full_repo}` (`{repo_path}`)\n"
        f"- Workstream tenant: `{tenant}`\n"
        f"{stamp}"
        f"- Arena manifest: `{manifest_path}`\n"
        f"{outputs}\n"
        f"## Candidates\n" + "\n".join(lines) + "\n\n"
        f"## Request\n{request}\n\n"
        f"{barrier}"
        f"## Hand-Grafted Synthesis\n"
        f"Follow the preloaded `{skill}` skill as the independent cross-vendor judge. Do not pick one candidate "
        f"wholesale and do not concatenate them.\n"
        f"1. Read every candidate artifact listed above in full.\n"
        f"2. For EVERY candidate, cite at least one concrete strength and one concrete risk (slot + section).\n"
        f"3. Hand-graft the canonical artifact section by section from the strongest candidate material, and "
        f"reconcile conflicts explicitly instead of averaging them.\n"
        f"4. Write {canonical}, ending with an `## Arena Synthesis` section that lists each candidate's strengths "
        f"and risks and which candidate every grafted section came from.\n"
        f"5. Log the outcome with `factonio_log_decision` (decision `arena_synthesis`, phase `{target}`, tenant "
        f"`{tenant}`, result `synthesized`, evidence: `arena_id`, `candidates`, `grafted_from`, `strengths`, "
        f"`risks`, `canonical_path`).\n"
        f"6. Complete with metadata `arena_id: {arena_id}`, `canonical_path: {canonical_path}`, and the "
        f"`arena_synthesis` decision id.\n"
    )


def _create_judge_card(
    conn: Any,
    *,
    target: str,
    arena_id: str,
    judge: dict[str, Any],
    candidates: list[dict[str, Any]],
    repo: dict[str, Any],
    tenant: str,
    manifest_path: Path,
    request: str,
    short_title: str,
    session_id: Optional[str],
    board: Optional[str],
) -> str:
    from hermes_cli import kanban_db as kb

    canonical_spec = spec_file_path(tenant)
    body = _judge_body(
        target=target,
        arena_id=arena_id,
        judge=judge,
        candidates=candidates,
        full_repo=str(repo["repo"]),
        repo_path=str(repo["path"]),
        tenant=tenant,
        manifest_path=manifest_path,
        request=request,
        canonical_spec=canonical_spec,
        canonical_holdout=holdout_dir_path(tenant) if target == "define" else None,
        canonical_design=design_dir_path(tenant) if target == "design" else None,
    )
    return kb.create_task(
        conn,
        title=f"[{target}] [arena:judge] {short_title}"[:120],
        body=body,
        assignee=WORKER_PROFILE,
        created_by=PLUGIN_ID,
        workspace_kind="dir",
        workspace_path=str(repo["path"]),
        project_id="",
        tenant=tenant,
        parents=[c["task_id"] for c in candidates],
        idempotency_key=f"arena:{arena_id}:judge",
        skills=[f"factonio:{target}"],
        model_override=judge.get("model") or None,
        provider_override=judge.get("provider") or None,
        goal_mode=True,
        session_id=session_id,
        board=board,
        completion_contract="local-only",
    )


def _canonical_artifact_path(tenant: str, target: str) -> Path:
    runs = _runs_root() / _safe_tenant(tenant)
    return runs / "spec.md" if target == "define" else runs / "design" / "DESIGN.md"


def _sha256(path: Path) -> Optional[str]:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _canonical_baseline(path: Path) -> dict[str, Any]:
    """Pre-arena state of the canonical artifact, so a stale pre-existing file never reads as synthesized."""
    try:
        st = path.stat()
    except OSError:
        return {"existed": False, "size": None, "mtime_ns": None, "sha256": None}
    if not path.is_file():
        return {"existed": False, "size": None, "mtime_ns": None, "sha256": None}
    return {"existed": True, "size": int(st.st_size), "mtime_ns": int(st.st_mtime_ns), "sha256": _sha256(path)}


def spawn_arena(
    repo_spec: str,
    tenant: Optional[str],
    request: str,
    *,
    target: str = "define",
    title: Optional[str] = None,
    candidate_models: Optional[list[Any]] = None,
    judge_model: Any = None,
    auto_judge: bool = True,
    strict_cross_vendor: bool = False,
    parents: Optional[list[str]] = None,
    session_id: Optional[str] = None,
    board: Optional[str] = None,
) -> dict[str, Any]:
    """Fan out competing ``define`` or ``design`` candidates across vendors plus a dependent judge.

    Fails closed with ``ValueError`` before any directory or card is created when ``target`` is
    unknown, the repo is unregistered, the candidate panel has fewer than 2 members across 2
    distinct known vendor families, or ``strict_cross_vendor`` and the judge is not provably from
    another vendor family. ``tenant`` may be empty to derive a new workstream tenant.
    """
    from hermes_cli.kanban_db_connect import connect_closing

    canon_target = str(target or "define").strip().lower()
    if canon_target not in ARENA_TARGETS:
        raise ValueError(f"target must be one of {', '.join(ARENA_TARGETS)}; got {target!r}")
    request_text = str(request or "").strip()
    if not request_text:
        raise ValueError("request must describe the contested spec or design.")
    repo = get_repo(repo_spec)
    if repo is None:
        raise ValueError(
            f"Repository {repo_spec!r} is not registered with Factonio. "
            "Run `hermes factonio add owner/repo` or call `factonio_add_repo` first."
        )
    slug = str(repo["slug"])
    panel = resolve_verify_panel(repo_slug=slug, override=candidate_models)
    if not panel["valid"]:
        raise ValueError(_diversity_error(panel))
    candidate_vendors = {str(e["vendor"]) for e in panel["entries"] if e.get("vendor")}
    judge = _resolve_judge(
        repo_slug=slug,
        target=canon_target,
        judge_model=judge_model,
        candidate_vendors=candidate_vendors,
        strict_cross_vendor=bool(strict_cross_vendor),
        has_unknown_candidate=any(not e.get("vendor") for e in panel["entries"]),
    )

    short_title = single_line(title or request_text.splitlines()[0])[:72].strip() or "arena"
    tenant = str(tenant or "").strip() or make_tenant(slug, label=short_title)
    arena_id = f"ar_{uuid.uuid4().hex[:10]}"
    arena_dir = _arena_dir(tenant, arena_id)
    manifest_path = arena_dir / MANIFEST_NAME
    canonical_spec = spec_file_path(tenant)
    full_repo = str(repo["repo"])
    repo_path = str(repo["path"])

    created_at_ns = time.time_ns()
    canonical: dict[str, Any] = {"spec_path": str(canonical_spec)}
    if canon_target == "define":
        canonical["holdout_dir"] = str(holdout_dir_path(tenant))
    else:
        canonical["design_dir"] = str(design_dir_path(tenant))
    canonical_baseline = _canonical_baseline(_canonical_artifact_path(tenant, canon_target))

    advisories = list(panel.get("advisories") or [])
    if judge.get("advisory"):
        advisories.append(judge["advisory"])

    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import write_txn

    effective_session_id = _resolve_calling_session_id(session_id)
    judge_task_id: Optional[str] = None
    candidates: list[dict[str, Any]] = []
    committed = False
    try:
        for idx, entry in enumerate(panel["entries"], start=1):
            slot = _slot_name(idx, entry.get("vendor"))
            slot_dir = arena_dir / "candidates" / slot
            cand: dict[str, Any] = {
                "slot": slot,
                "vendor": entry.get("vendor"),
                "provider": entry.get("provider"),
                "model": entry["model"],
                "label": entry["label"],
                "dir": str(slot_dir),
            }
            if canon_target == "define":
                cand["spec_path"] = str(slot_dir / "spec.md")
                cand["holdout_dir"] = str(slot_dir / "holdout")
                (slot_dir / "holdout").mkdir(parents=True, exist_ok=True)
            else:
                cand["design_dir"] = str(slot_dir / "design")
                (slot_dir / "design").mkdir(parents=True, exist_ok=True)
            candidates.append(cand)

        with connect_closing(board=board) as conn:
            # One outer transaction: a failure on any card, or on the manifest write below, rolls
            # back every card so the dispatcher never sees a partial arena.
            with write_txn(conn):
                for cand in candidates:
                    body = _candidate_body(
                        target=canon_target,
                        arena_id=arena_id,
                        cand=cand,
                        full_repo=full_repo,
                        repo_path=repo_path,
                        tenant=tenant,
                        arena_dir=arena_dir,
                        request=request_text,
                        canonical_spec=canonical_spec,
                    )
                    vendor_tag = _stamp_token(cand["vendor"], "unknown")
                    cand["task_id"] = kb.create_task(
                        conn,
                        title=f"[{canon_target}] [arena:{vendor_tag}] {short_title}"[:120],
                        body=body,
                        assignee=WORKER_PROFILE,
                        created_by=PLUGIN_ID,
                        workspace_kind="dir",
                        workspace_path=repo_path,
                        project_id="",
                        tenant=tenant,
                        parents=list(parents or []),
                        idempotency_key=f"arena:{arena_id}:{cand['slot']}",
                        skills=[f"factonio:{canon_target}"],
                        model_override=cand["model"],
                        provider_override=cand.get("provider") or None,
                        goal_mode=True,
                        session_id=effective_session_id,
                        board=board,
                        completion_contract="local-only",
                    )
                if auto_judge:
                    judge_task_id = _create_judge_card(
                        conn,
                        target=canon_target,
                        arena_id=arena_id,
                        judge=judge,
                        candidates=candidates,
                        repo=repo,
                        tenant=tenant,
                        manifest_path=manifest_path,
                        request=request_text,
                        short_title=short_title,
                        session_id=effective_session_id,
                        board=board,
                    )
                judge_info = {k: judge[k] for k in ("provider", "model", "label", "vendor", "source", "cross_vendor")}
                judge_info["task_id"] = judge_task_id
                manifest = {
                    "arena_id": arena_id,
                    "tenant": tenant,
                    "repo": full_repo,
                    "repo_slug": slug,
                    "target": canon_target,
                    "title": short_title,
                    "request": request_text,
                    "created_at": created_at_ns // 1_000_000_000,
                    "created_at_ns": created_at_ns,
                    "panel_source": panel["source"],
                    "vendors": list(panel["vendors"]),
                    "strict_cross_vendor": bool(strict_cross_vendor),
                    "parents": list(parents or []),
                    "candidates": candidates,
                    "judge": judge_info,
                    "canonical": canonical,
                    "canonical_baseline": canonical_baseline,
                    "advisories": advisories,
                }
                _write_json_atomic(manifest_path, manifest)
            committed = True
            # Notify subscriptions open their own (non-nestable) write transaction, so they run
            # after the cards are committed; a failed subscription only reports subscribed=false.
            all_subscribed = True
            for task_id in [c["task_id"] for c in candidates] + ([judge_task_id] if judge_task_id else []):
                all_subscribed = _auto_subscribe_task(conn, task_id, session_id=effective_session_id) and all_subscribed
    except BaseException:
        if not committed:
            shutil.rmtree(arena_dir, ignore_errors=True)
        raise

    record_transition(
        "spawn_arena",
        tenant=tenant,
        repo_slug=slug,
        phase=canon_target,
        card_id=judge_task_id or candidates[0]["task_id"],
        why=f"Arena {arena_id} ({canon_target}) across {', '.join(panel['vendors'])}: {short_title}",
        evidence={
            "arena_id": arena_id,
            "target": canon_target,
            "candidates": [{k: c.get(k) for k in ("slot", "vendor", "model", "task_id")} for c in candidates],
            "candidate_task_ids": [c["task_id"] for c in candidates],
            "judge_task_id": judge_task_id,
            "judge_vendor": judge.get("vendor"),
            "judge_model": judge.get("label"),
            "judge_cross_vendor": judge["cross_vendor"],
            "strict_cross_vendor": bool(strict_cross_vendor),
            "panel_source": panel["source"],
            "manifest_path": str(manifest_path),
        },
        result="spawned",
    )

    return {
        "ok": True,
        "arena_id": arena_id,
        "tenant": tenant,
        "repo": full_repo,
        "repo_slug": slug,
        "target": canon_target,
        "arena_dir": str(arena_dir),
        "manifest_path": str(manifest_path),
        "candidates": candidates,
        "candidate_task_ids": [c["task_id"] for c in candidates],
        "judge": judge_info,
        "judge_task_id": judge_task_id,
        "canonical": canonical,
        "vendors": list(panel["vendors"]),
        "panel_source": panel["source"],
        "advisories": advisories,
        "subscribed": bool(all_subscribed),
    }


# --- read-only evaluation ---------------------------------------------------------------------


def _load_manifest(path: Path) -> Optional[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("arena_id") else None


def _find_manifest(arena_id: Optional[str], tenant: Optional[str]) -> tuple[Optional[Path], Optional[str]]:
    """Locate a manifest by id (optionally within a tenant) or the newest one for a tenant."""
    root = _runs_root()
    if arena_id:
        if not ARENA_ID_RE.match(arena_id):
            return None, f"Invalid arena_id {arena_id!r} (expected ar_<hex>)."
        tenants = [_safe_tenant(tenant)] if tenant else sorted(p.name for p in root.glob("*") if p.is_dir())
        for name in tenants:
            path = root / name / "arena" / arena_id / MANIFEST_NAME
            if path.is_file():
                return path, None
        return None, f"No arena found for {arena_id}."
    if not tenant:
        return None, "Pass arena_id and/or tenant to select an arena."
    newest: Optional[tuple[tuple[int, int, str], Path]] = None
    for path in (root / _safe_tenant(tenant) / "arena").glob(f"ar_*/{MANIFEST_NAME}"):
        data = _load_manifest(path)
        if data is None:
            continue
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            mtime_ns = 0
        key = (_created_ns(data), int(mtime_ns), str(data.get("arena_id")))
        if newest is None or key > newest[0]:
            newest = (key, path)
    if newest is None:
        return None, f"No arena found for tenant={tenant}."
    return newest[1], None


def _created_ns(manifest: dict[str, Any]) -> int:
    for key, scale in (("created_at_ns", 1), ("created_at", 1_000_000_000)):
        raw = manifest.get(key)
        if isinstance(raw, bool) or (isinstance(raw, float) and not math.isfinite(raw)):
            continue
        try:
            value = int(raw or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if value > 0:
            return value * scale
    return 0


def _read_tasks(db_path: Path, ids: list[str], arena_id: str) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], Optional[str]]:
    """``(tasks_by_id, judge_cards, error)`` from a ``mode=ro`` connection (never creates or writes)."""
    if not db_path.is_file():
        return {}, [], f"kanban.db not found at {db_path}"
    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=3.0)
    except sqlite3.Error as exc:
        return {}, [], f"kanban.db unreadable: {exc}"
    conn.row_factory = sqlite3.Row
    try:
        cols = "id, title, status, created_at, completed_at, model_override"
        rows: list[Any] = []
        if ids:
            marks = ",".join("?" for _ in ids)
            rows = conn.execute(f"SELECT {cols} FROM tasks WHERE id IN ({marks})", ids).fetchall()
        judges = conn.execute(
            f"SELECT {cols} FROM tasks WHERE body LIKE ? AND status != 'archived' ORDER BY created_at, rowid",
            (f"%- Arena: `{arena_id}` (role=`judge`%",),
        ).fetchall()
    except sqlite3.Error as exc:
        return {}, [], f"kanban.db unreadable: {exc}"
    finally:
        conn.close()
    return {str(r["id"]): dict(r) for r in rows}, [dict(r) for r in judges], None


def _file_info(path: Path) -> dict[str, Any]:
    try:
        st = path.stat()
        exists = path.is_file()
        size = int(st.st_size) if exists else 0
    except OSError:
        exists, size = False, 0
    return {"path": str(path), "exists": exists, "size_bytes": size}


def _canonical_changed(path: Path, manifest: dict[str, Any]) -> bool:
    """True when the canonical artifact was written after the arena was spawned."""
    raw = manifest.get("canonical_baseline")
    baseline: dict[str, Any] = raw if isinstance(raw, dict) else {}
    try:
        mtime_ns = int(path.stat().st_mtime_ns)
    except OSError:
        return False
    digest = _sha256(path)
    if digest is None:
        # An unreadable canonical artifact cannot prove it was synthesized; fail closed.
        return False
    if baseline and not baseline.get("existed"):
        return True
    if baseline.get("sha256") and digest != baseline["sha256"]:
        return True
    return mtime_ns > max(int(baseline.get("mtime_ns") or 0), _created_ns(manifest))


def _count_files(directory: Path) -> int:
    try:
        return sum(1 for p in directory.rglob("*") if p.is_file())
    except OSError:
        return 0


def _evaluate_manifest_state(
    manifest: dict[str, Any],
    tasks_by_id: dict[str, dict[str, Any]],
    judge_rows: list[dict[str, Any]],
    db_error: Optional[str],
    *,
    include_paths: bool,
) -> dict[str, Any]:
    """Pure arena status/synthesis calculation shared by `evaluate_arena` and `list_arena_summaries`.

    With ``include_paths=False`` the result carries no filesystem path, holdout file count, or
    path-bearing advisory, so it is safe for Dashboard payloads.
    """
    target = str(manifest.get("target") or "define")
    arena_tenant = str(manifest.get("tenant") or "")
    cand_meta = [c for c in manifest.get("candidates") or [] if isinstance(c, dict)]
    advisories: list[str] = [db_error] if db_error else []

    candidates: list[dict[str, Any]] = []
    for c in cand_meta:
        task = tasks_by_id.get(str(c.get("task_id") or ""))
        status = str(task["status"]) if task else "missing"
        extra: dict[str, Any] = {}
        if target == "define":
            artifact = _file_info(Path(str(c.get("spec_path") or Path(str(c.get("dir"))) / "spec.md")))
            if include_paths:
                extra["holdout_files"] = _count_files(Path(str(c.get("holdout_dir") or Path(str(c.get("dir"))) / "holdout")))
        else:
            artifact = _file_info(Path(str(c.get("design_dir") or Path(str(c.get("dir"))) / "design")) / "DESIGN.md")
        has_artifact = artifact["exists"] and artifact["size_bytes"] > 0
        if status == "missing":
            advisories.append(f"Candidate {c.get('slot')} card {c.get('task_id')} is missing from kanban.db.")
        elif status in _GONE:
            advisories.append(f"Candidate {c.get('slot')} card {c.get('task_id')} is {status}; the arena cannot be synthesized without it.")
        elif status in _FAILED:
            advisories.append(f"Candidate {c.get('slot')} card {c.get('task_id')} ended {status}; re-run or replace it before synthesis.")
        elif status == _DONE and not has_artifact:
            where = f" {artifact['path']}" if include_paths else ""
            advisories.append(f"Candidate {c.get('slot')} is done but its artifact{where} is missing or empty.")
        entry: dict[str, Any] = {
            "slot": c.get("slot"),
            "vendor": c.get("vendor"),
            "model": c.get("label") or c.get("model"),
            "task_id": c.get("task_id"),
            "status": status,
        }
        if include_paths:
            entry["artifact"] = artifact
        entry["artifact_ready"] = bool(status == _DONE and has_artifact)
        entry.update(extra)
        candidates.append(entry)

    raw_judge = manifest.get("judge")
    manifest_judge: dict[str, Any] = raw_judge if isinstance(raw_judge, dict) else {}
    judge_row: Optional[dict[str, Any]] = next((r for r in judge_rows if r["id"] == manifest_judge.get("task_id")), None)
    judge_row = judge_row or (judge_rows[-1] if judge_rows else None)

    all_present = bool(cand_meta) and all(c["status"] not in _GONE for c in candidates)
    any_failed = any(c["status"] in _FAILED for c in candidates)

    judge_out: Optional[dict[str, Any]] = None
    if judge_row is not None:
        same = manifest_judge.get("task_id") == judge_row["id"]
        judge_out = {
            "task_id": judge_row["id"],
            "status": str(judge_row.get("status") or "unknown"),
            "vendor": manifest_judge.get("vendor") if same else None,
            "model": (manifest_judge.get("label") or manifest_judge.get("model")) if same else judge_row.get("model_override"),
            "cross_vendor": manifest_judge.get("cross_vendor") if same else None,
        }
    elif manifest_judge.get("task_id"):
        advisories.append(f"Judge card {manifest_judge['task_id']} is missing from kanban.db.")

    canonical_path = _canonical_artifact_path(arena_tenant, target)
    canonical_info = _file_info(canonical_path)
    canonical_exists = bool(canonical_info["exists"] and canonical_info["size_bytes"] > 0)
    canonical_fresh = canonical_exists and _canonical_changed(canonical_path, manifest)
    canonical_where = f" {canonical_info['path']}" if include_paths else ""

    ready = bool(candidates) and all(c["artifact_ready"] for c in candidates)
    judge_done = bool(judge_out and judge_out["status"] == _DONE)
    synthesized = bool(ready and all_present and not any_failed and not db_error and judge_done and canonical_fresh)
    if judge_done and not synthesized:
        if not (ready and all_present and not any_failed):
            advisories.append("Judge is done but not every candidate finished with a non-empty artifact; the synthesis is not trusted.")
        if not canonical_exists:
            advisories.append(f"Judge is done but the canonical artifact{canonical_where} is missing or empty.")
        elif not canonical_fresh:
            advisories.append(
                f"Judge is done but the canonical artifact{canonical_where} is unchanged since before the arena was spawned."
            )

    judge_failed = bool(judge_out and judge_out["status"] in _FAILED)
    if judge_failed:
        advisories.append(
            f"Judge card {judge_out['task_id']} ended in terminal state '{judge_out['status']}'; "
            "re-run or replace it before synthesis."
        )

    if synthesized:
        status = "synthesized"
    elif not all_present or db_error:
        status = "degraded"
    elif any_failed or judge_failed:
        status = "needs_attention"
    elif any(c["status"] in _STUCK for c in candidates) or (judge_out and judge_out["status"] in _STUCK):
        status = "blocked"
    elif judge_done:
        status = "needs_attention"
    elif not all(c["status"] == _DONE for c in candidates):
        status = "candidates_in_progress"
    elif not ready:
        status = "needs_attention"
    elif judge_out:
        status = "judging"
    else:
        status = "ready_for_synthesis"

    state: dict[str, Any] = {
        "status": status,
        "candidates": candidates,
        "judge": judge_out,
        "ready_for_synthesis": ready,
        "synthesized": synthesized,
        "canonical_artifact_exists": canonical_exists,
        "advisories": advisories,
    }
    if include_paths:
        state["canonical_artifact"] = canonical_info
    return state


def evaluate_arena(
    *,
    arena_id: Optional[str] = None,
    tenant: Optional[str] = None,
    spawn_judge: bool = False,
    judge_model: Any = None,
    strict_cross_vendor: bool = False,
    session_id: Optional[str] = None,
    board: Optional[str] = None,
) -> dict[str, Any]:
    """Report an arena's progress from read-only `kanban.db` plus on-disk candidate artifacts.

    With ``spawn_judge`` (and no live judge card yet) it also creates the dependent judge card,
    which is the only write this function performs.
    """
    aid = str(arena_id or "").strip() or None
    manifest_path, error = _find_manifest(aid, str(tenant or "").strip() or None)
    if manifest_path is None:
        return {"ok": False, "error": error, "arena_id": aid}
    manifest = _load_manifest(manifest_path)
    if manifest is None:
        return {"ok": False, "error": f"Arena manifest unreadable: {manifest_path}", "arena_id": aid}

    aid = str(manifest["arena_id"])
    target = str(manifest.get("target") or "define")
    arena_tenant = str(manifest.get("tenant") or "")
    cand_meta = [c for c in manifest.get("candidates") or [] if isinstance(c, dict)]
    ids = [str(c["task_id"]) for c in cand_meta if c.get("task_id")]
    db_path = _kanban_db_file(board)
    tasks, judge_rows, db_error = _read_tasks(db_path, ids, aid)
    state = _evaluate_manifest_state(manifest, tasks, judge_rows, db_error, include_paths=True)
    extra_advisories: list[str] = []
    statuses = [c["status"] for c in state["candidates"]]
    all_present = bool(cand_meta) and all(s not in _GONE for s in statuses)
    any_failed = any(s in _FAILED for s in statuses)

    spawned_judge = False
    if spawn_judge and state["judge"] is None:
        if db_error or not all_present or any_failed:
            extra_advisories.append(
                "Judge not spawned: every candidate card must exist in kanban.db and none may be archived, failed or cancelled."
            )
        else:
            raw_judge = manifest.get("judge")
            manifest_judge: dict[str, Any] = raw_judge if isinstance(raw_judge, dict) else {}
            repo = get_repo(str(manifest.get("repo_slug") or manifest.get("repo") or ""))
            if repo is None:
                return {"ok": False, "error": f"Repository {manifest.get('repo')!r} is no longer registered.", "arena_id": aid}
            effective_strict = bool(strict_cross_vendor) or bool(manifest.get("strict_cross_vendor"))
            vendors = {str(c["vendor"]) for c in cand_meta if c.get("vendor")}
            judge = _resolve_judge(
                repo_slug=str(repo["slug"]),
                target=target,
                judge_model=judge_model if judge_model not in (None, "", {}) else None,
                candidate_vendors=vendors,
                strict_cross_vendor=effective_strict,
                has_unknown_candidate=any(not c.get("vendor") for c in cand_meta),
                saved_judge=manifest_judge,
            )
            from hermes_cli.kanban_db_connect import connect_closing, write_txn

            effective_session_id = _resolve_calling_session_id(session_id)
            judge_key = f"arena:{aid}:judge"
            try:
                prior_manifest: Optional[bytes] = manifest_path.read_bytes()
            except OSError:
                prior_manifest = None
            updated_manifest: Optional[dict[str, Any]] = None
            committed = False
            try:
                with connect_closing(board=board) as conn:
                    # The manifest write happens inside the judge card's transaction so a failed write
                    # rolls the card back: kanban.db and manifest.json must never diverge.
                    with write_txn(conn):
                        existing = conn.execute(
                            "SELECT id FROM tasks WHERE idempotency_key = ? AND status != 'archived' LIMIT 1", (judge_key,)
                        ).fetchone()
                        judge_id = _create_judge_card(
                            conn,
                            target=target,
                            arena_id=aid,
                            judge=judge,
                            candidates=cand_meta,
                            repo=repo,
                            tenant=arena_tenant,
                            manifest_path=manifest_path,
                            request=str(manifest.get("request") or manifest.get("title") or "").strip(),
                            short_title=str(manifest.get("title") or "arena"),
                            session_id=effective_session_id,
                            board=board,
                        )
                        row = conn.execute("SELECT status, model_override FROM tasks WHERE id = ?", (judge_id,)).fetchone()
                        if existing is None:
                            updated_manifest = {
                                **manifest,
                                "judge": {
                                    **{k: judge[k] for k in ("provider", "model", "label", "vendor", "source", "cross_vendor")},
                                    "task_id": judge_id,
                                },
                                "strict_cross_vendor": effective_strict,
                            }
                            _write_json_atomic(manifest_path, updated_manifest)
                    committed = True
                    created = existing is None
                    if created:
                        _auto_subscribe_task(conn, judge_id, session_id=effective_session_id)
            except BaseException:
                if not committed and updated_manifest is not None and prior_manifest is not None:
                    try:
                        if manifest_path.read_bytes() != prior_manifest:
                            _write_bytes_atomic(manifest_path, prior_manifest)
                    except OSError:
                        pass
                raise
            judge_rows = [{"id": judge_id, "status": str(row[0]) if row else "unknown", "model_override": row[1] if row else None}]
            if created and updated_manifest is not None:
                if judge.get("advisory"):
                    extra_advisories.append(judge["advisory"])
                manifest = updated_manifest
                record_transition(
                    "spawn_arena_judge",
                    tenant=arena_tenant,
                    repo_slug=str(repo["slug"]),
                    phase=target,
                    card_id=judge_id,
                    why=f"Arena {aid} judge spawned on demand",
                    evidence={
                        "arena_id": aid,
                        "judge_task_id": judge_id,
                        "candidate_task_ids": ids,
                        "judge_vendor": judge.get("vendor"),
                        "judge_model": judge.get("label"),
                        "judge_source": judge.get("source"),
                        "judge_cross_vendor": judge["cross_vendor"],
                        "strict_cross_vendor": effective_strict,
                    },
                    result="spawned",
                )
                spawned_judge = True
            else:
                extra_advisories.append(f"Judge card {judge_id} already exists; not spawning another.")
            state = _evaluate_manifest_state(manifest, tasks, judge_rows, db_error, include_paths=True)
    elif spawn_judge:
        extra_advisories.append(f"Judge card {state['judge']['task_id']} already exists; not spawning another.")

    return {
        "ok": True,
        "arena_id": aid,
        "tenant": arena_tenant,
        "repo": manifest.get("repo"),
        "target": target,
        "status": state["status"],
        "candidates": state["candidates"],
        "judge": state["judge"],
        "ready_for_synthesis": state["ready_for_synthesis"],
        "synthesized": state["synthesized"],
        "canonical_artifact_exists": state["canonical_artifact_exists"],
        "canonical_artifact": state["canonical_artifact"],
        "spawned_judge": spawned_judge,
        "manifest_path": str(manifest_path),
        "advisories": state["advisories"] + extra_advisories,
    }


# --- read-only Dashboard summaries ------------------------------------------------------------

_SUMMARY_COLS = "id, title, status, created_at, completed_at, model_override"
_JUDGE_STAMP_LIKE = "%(role=`judge`%"


def _read_arena_tasks(
    db_path: Path, ids: list[str], arena_ids: list[str]
) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]], Optional[str]]:
    """One ``mode=ro`` query for every candidate id and judge stamp across ``arena_ids`` (never writes).

    Error strings carry no filesystem path, so they are safe for Dashboard payloads.
    """
    if not db_path.is_file():
        return {}, {}, "kanban.db not found"
    clauses = ["(status != 'archived' AND body LIKE ?)"]
    params: list[Any] = [_JUDGE_STAMP_LIKE]
    if ids:
        clauses.append(f"id IN ({','.join('?' for _ in ids)})")
        params.extend(ids)
    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=3.0)
    except sqlite3.Error:
        return {}, {}, "kanban.db unreadable"
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"SELECT {_SUMMARY_COLS}, body FROM tasks WHERE {' OR '.join(clauses)} ORDER BY created_at, rowid",
            params,
        ).fetchall()
    except sqlite3.Error:
        return {}, {}, "kanban.db unreadable"
    finally:
        conn.close()
    wanted = set(ids)
    tasks: dict[str, dict[str, Any]] = {}
    judges: dict[str, list[dict[str, Any]]] = {aid: [] for aid in arena_ids}
    for r in rows:
        row = {k: r[k] for k in r.keys() if k != "body"}
        if str(r["id"]) in wanted:
            tasks[str(r["id"])] = row
        if str(r["status"]) == "archived":
            continue
        body = str(r["body"] or "")
        for aid in arena_ids:
            if f"- Arena: `{aid}` (role=`judge`" in body:
                judges[aid].append(row)
    return tasks, judges, None


def _scrub(text: Any) -> Optional[str]:
    """Public string fields are a real ``str`` without a holdout reference, else ``None``."""
    return text if isinstance(text, str) and not contains_holdout_reference(text) else None


def _public_candidate(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        **{k: _scrub(entry.get(k)) for k in ("slot", "vendor", "model", "task_id", "status")},
        "artifact_ready": entry.get("artifact_ready") is True,
    }


def _public_judge(judge: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Typed judge projection: manifest data never reaches the payload except as ``str``/``bool``/``None``."""
    if not judge:
        return None
    cross_vendor = judge.get("cross_vendor")
    return {
        **{k: _scrub(judge.get(k)) for k in ("task_id", "status", "vendor", "model")},
        "cross_vendor": cross_vendor if isinstance(cross_vendor, bool) else None,
    }


# Per-manifest failures: corrupt JSON shapes, non-finite numbers, unreadable files.
_MANIFEST_ERRORS = (TypeError, ValueError, KeyError, AttributeError, OSError, OverflowError)


def _public_manifest_ok(manifest: dict[str, Any], tenant: str, arena_id: str) -> bool:
    """Only manifests whose identity matches their ``<tenant>/arena/<arena_id>`` directory reach Dashboard payloads."""
    raw_tenant = manifest.get("tenant")
    return (
        str(manifest.get("arena_id")) == arena_id
        and isinstance(raw_tenant, str)
        and bool(raw_tenant)
        and _safe_tenant(raw_tenant) == tenant
        and (manifest.get("target") or "define") in ARENA_TARGETS
        and not any(isinstance(manifest.get(k), float) and not math.isfinite(manifest[k]) for k in ("created_at_ns", "created_at"))
        and not contains_holdout_reference(raw_tenant)
        and not contains_holdout_reference(arena_id)
    )


def list_arena_summaries(
    *, tenants: Iterable[str], db_path: Optional[Path] = None, board: Optional[str] = None
) -> list[dict[str, Any]]:
    """Dashboard-safe summaries of every arena of ``tenants``, newest first.

    Strictly read-only: never creates directories, `kanban.db`, or manifests, and reads `kanban.db`
    with a single ``mode=ro`` query. Corrupt or unreadable manifests are skipped. Summaries never
    carry filesystem paths, holdout counts, or any string that references holdout.
    """
    root = _runs_root()
    found: list[tuple[tuple[int, int, str], dict[str, Any]]] = []
    for name in sorted({_safe_tenant(str(t)) for t in tenants if t}):
        arena_root = root / name / "arena"
        try:
            if not arena_root.is_dir():
                continue
            entries = sorted(arena_root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not ARENA_ID_RE.match(entry.name):
                continue
            path = entry / MANIFEST_NAME
            manifest = _load_manifest(path)
            try:
                if manifest is None or not _public_manifest_ok(manifest, name, entry.name):
                    continue
                found.append(((_created_ns(manifest), int(path.stat().st_mtime_ns), entry.name), manifest))
            except _MANIFEST_ERRORS:
                continue
    if not found:
        return []
    found.sort(key=lambda item: item[0], reverse=True)

    arena_ids = [m["arena_id"] for _, m in found]
    ids = sorted({
        str(c["task_id"])
        for _, m in found
        for c in (m.get("candidates") if isinstance(m.get("candidates"), list) else [])
        if isinstance(c, dict) and c.get("task_id")
    })
    kanban = Path(db_path) if db_path is not None else _kanban_db_file(board)
    tasks, judges, db_error = _read_arena_tasks(kanban, ids, arena_ids)

    summaries: list[dict[str, Any]] = []
    for _, manifest in found:
        aid = str(manifest["arena_id"])
        try:
            state = _evaluate_manifest_state(manifest, tasks, judges.get(aid, []), db_error, include_paths=False)
            summaries.append({
                "arena_id": aid,
                "tenant": str(manifest["tenant"]),
                "repo": _scrub(manifest.get("repo")),
                "target": str(manifest.get("target") or "define"),
                "title": _scrub(manifest.get("title")),
                "status": str(state["status"]),
                "created_at": int(_created_ns(manifest) // 1_000_000_000) or None,
                "candidates": [_public_candidate(c) for c in state["candidates"]],
                "judge": _public_judge(state["judge"]),
                "ready_for_synthesis": state["ready_for_synthesis"] is True,
                "synthesized": state["synthesized"] is True,
                "canonical_artifact_exists": state["canonical_artifact_exists"] is True,
                "advisories": [a for a in state["advisories"] if isinstance(a, str) and not contains_holdout_reference(a)],
            })
        except _MANIFEST_ERRORS:
            continue
    return summaries
