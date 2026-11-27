"""Clean-room tests for the opt-in `/arena` competing-candidate mode (Wave 5 Unit 5)."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import unittest
from pathlib import Path
from typing import Any, Optional

from tests._env import PLUGIN_ROOT, isolated_hermes_home
from tests.test_verify_panel import _cli, _complete, _get_task, _register, _set_repo_field, _task_count, _tools, _trail_rows

if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

TWO_VENDORS = ["anthropic/claude-opus-4-6", "openai/gpt-5"]
SAME_VENDOR = ["openrouter/anthropic/claude-opus-4-6", "anthropic/claude-sonnet-4-6"]
GOOGLE_JUDGE = "google/gemini-2.5-pro"


def _parents(task_id: str) -> list[str]:
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing

    with connect_closing() as conn:
        return sorted(kb.parent_ids(conn, task_id))


def _arena_root(home: Path) -> Path:
    return home / "plugin-data" / "factonio" / "runs"


def _arena_dirs(home: Path) -> list[Path]:
    return sorted(_arena_root(home).glob("*/arena/ar_*"))


def _db_fingerprint(home: Path) -> tuple[str, ...]:
    """Main DB bytes plus WAL frames; an empty `-wal` a `mode=ro` reader may leave holds no writes."""
    out = []
    for suffix in ("", "-wal"):
        path = home / f"kanban.db{suffix}"
        data = path.read_bytes() if path.is_file() else b""
        out.append(hashlib.sha256(data).hexdigest() if data else "-")
    return tuple(out)


def _spawn(target: str = "define", tenant: Optional[str] = "acme-svc-arena", **kw: Any) -> dict[str, Any]:
    from factonio.arena import spawn_arena

    kw.setdefault("candidate_models", TWO_VENDORS)
    kw.setdefault("judge_model", GOOGLE_JUDGE)
    return spawn_arena("acme/svc", tenant, "Design the webhook retry policy.", target=target, title="Retry policy", **kw)


class TestArenaFailsClosed(unittest.TestCase):
    def _assert_nothing_created(self, home: Path) -> None:
        self.assertEqual(_task_count(), 0)
        self.assertEqual(_arena_dirs(home), [])
        self.assertEqual(_trail_rows("spawn_arena"), [])

    def test_single_vendor_or_too_few_candidates_creates_zero_cards(self):
        with isolated_hermes_home() as home:
            _register(home)
            for models in (SAME_VENDOR, ["openai/gpt-5"], ["anthropic/claude-opus-4-6", "mystery-model-x"]):
                with self.assertRaisesRegex(ValueError, "2 distinct known vendor families"):
                    _spawn(candidate_models=models)
                self._assert_nothing_created(home)
            # No override and no configured verify_panel also fails closed.
            with self.assertRaisesRegex(ValueError, "No cards were created"):
                _spawn(candidate_models=None)
            self._assert_nothing_created(home)

    def test_invalid_target_and_unregistered_repo(self):
        from factonio.arena import spawn_arena

        with isolated_hermes_home() as home:
            _register(home)
            with self.assertRaisesRegex(ValueError, "target must be one of define, design"):
                _spawn(target="implement")
            with self.assertRaisesRegex(ValueError, "not registered"):
                spawn_arena("nope/missing", None, "x", candidate_models=TWO_VENDORS)
            self._assert_nothing_created(home)

    def test_strict_cross_vendor_rejects_overlapping_or_unknown_judge_before_creating_cards(self):
        with isolated_hermes_home() as home:
            _register(home)
            with self.assertRaisesRegex(ValueError, "shares vendor family openai"):
                _spawn(judge_model="openai/gpt-5-mini", strict_cross_vendor=True)
            self._assert_nothing_created(home)
            with self.assertRaisesRegex(ValueError, "no judge model is configured"):
                _spawn(judge_model=None, strict_cross_vendor=True)
            self._assert_nothing_created(home)
            with self.assertRaisesRegex(ValueError, "could not be inferred"):
                _spawn(judge_model="mystery-model-x", strict_cross_vendor=True)
            self._assert_nothing_created(home)

            res = _spawn(strict_cross_vendor=True)
            self.assertTrue(res["judge"]["cross_vendor"])
            self.assertEqual(res["judge"]["vendor"], "google")
            self.assertEqual(_task_count(), 3)

    def test_kanban_failure_before_any_card_removes_the_arena_directory(self):
        from unittest import mock

        from hermes_cli import kanban_db as kb

        with isolated_hermes_home() as home:
            _register(home)
            with mock.patch.object(kb, "create_task", side_effect=sqlite3.OperationalError("unable to open database file")):
                with self.assertRaises(sqlite3.OperationalError):
                    _spawn()
            self._assert_nothing_created(home)

    def test_non_strict_overlapping_judge_is_an_advisory(self):
        with isolated_hermes_home() as home:
            _register(home)
            res = _spawn(judge_model="anthropic/claude-haiku-4-5")
            self.assertFalse(res["judge"]["cross_vendor"])
            self.assertTrue(any("not provably cross-vendor" in a for a in res["advisories"]))


class TestDefineArena(unittest.TestCase):
    def test_candidates_isolated_judge_depends_on_all_and_trail_logged(self):
        from factonio.orchestrator import holdout_dir_path, spec_file_path

        with isolated_hermes_home() as home:
            _register(home)
            res = _spawn()
            self.assertTrue(res["ok"])
            arena_id, tenant = res["arena_id"], res["tenant"]
            self.assertRegex(arena_id, r"^ar_[0-9a-f]{10}$")
            self.assertEqual(tenant, "acme-svc-arena")
            self.assertEqual([c["slot"] for c in res["candidates"]], ["c1-anthropic", "c2-openai"])
            self.assertEqual(_task_count(), 3)

            arena_dir = _arena_root(home) / tenant / "arena" / arena_id
            self.assertEqual(Path(res["arena_dir"]), arena_dir)
            canonical_holdout = str(holdout_dir_path(tenant))
            for cand in res["candidates"]:
                slot_dir = arena_dir / "candidates" / cand["slot"]
                self.assertTrue((slot_dir / "holdout").is_dir())
                self.assertEqual(cand["spec_path"], str(slot_dir / "spec.md"))
                task = _get_task(cand["task_id"])
                self.assertEqual(task.title, f"[define] [arena:{cand['vendor']}] Retry policy")
                self.assertEqual(task.model_override, cand["model"])
                self.assertEqual(task.tenant, tenant)
                self.assertIn("factonio:define", task.skills)
                self.assertIn(
                    f"- Arena: `{arena_id}` (role=`candidate`, target=`define`, slot=`{cand['slot']}`, "
                    f"vendor=`{cand['vendor']}`, model=`{cand['label']}`)",
                    task.body,
                )
                self.assertIn(cand["spec_path"], task.body)
                self.assertIn(cand["holdout_dir"], task.body)
                self.assertNotIn(canonical_holdout, task.body)
                for other in res["candidates"]:
                    if other is not cand:
                        self.assertNotIn(other["dir"], task.body)

            judge = _get_task(res["judge_task_id"])
            self.assertEqual(judge.title, "[define] [arena:judge] Retry policy")
            self.assertEqual(_parents(res["judge_task_id"]), sorted(res["candidate_task_ids"]))
            self.assertEqual(judge.model_override, "gemini-2.5-pro")
            self.assertEqual(judge.provider_override, "google")
            self.assertIn(f"- Arena: `{arena_id}` (role=`judge`, target=`define`, vendor=`google`", judge.body)
            for needle in ("Hand-Grafted Synthesis", "strength", "risk", "arena_synthesis", "factonio_log_decision",
                           str(spec_file_path(tenant)), canonical_holdout, "Do not pick one candidate"):
                self.assertIn(needle, judge.body)
            for cand in res["candidates"]:
                self.assertIn(cand["spec_path"], judge.body)

            manifest = json.loads((arena_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["arena_id"], arena_id)
            self.assertEqual(manifest["judge"]["task_id"], res["judge_task_id"])
            self.assertEqual([c["task_id"] for c in manifest["candidates"]], res["candidate_task_ids"])
            self.assertEqual(list(arena_dir.glob(".manifest.json.*")), [])

            rows = _trail_rows("spawn_arena", tenant)
            self.assertEqual(len(rows), 1)
            ev = rows[0]["evidence"]
            self.assertEqual(rows[0]["phase"], "define")
            self.assertEqual((ev["arena_id"], ev["judge_task_id"]), (arena_id, res["judge_task_id"]))
            self.assertEqual(ev["candidate_task_ids"], res["candidate_task_ids"])
            self.assertTrue(ev["judge_cross_vendor"])

    def test_default_judge_prefers_a_vendor_no_candidate_uses(self):
        with isolated_hermes_home() as home:
            _register(home)
            _set_repo_field("acme-svc", models={"define": "anthropic/claude-opus-4-6", "verify": GOOGLE_JUDGE})
            res = _spawn(judge_model=None)
            self.assertEqual(res["judge"]["vendor"], "google")
            self.assertTrue(res["judge"]["cross_vendor"])
            self.assertEqual(res["judge"]["source"], "repo:acme-svc.models.verify")

    def test_parents_and_derived_tenant(self):
        with isolated_hermes_home() as home:
            _register(home)
            parent = _spawn(tenant="acme-svc-parent", auto_judge=False)["candidate_task_ids"][0]
            res = _spawn(tenant=None, parents=[parent])
            self.assertTrue(res["tenant"].startswith("acme-svc-retry-policy"))
            for task_id in res["candidate_task_ids"]:
                self.assertEqual(_parents(task_id), [parent])


class TestDesignArena(unittest.TestCase):
    def test_design_candidates_never_see_holdout(self):
        from factonio.orchestrator import design_dir_path, run_data_dir

        with isolated_hermes_home() as home:
            _register(home)
            res = _spawn(target="design")
            tenant = res["tenant"]
            arena_dir = Path(res["arena_dir"])
            self.assertEqual(list(arena_dir.rglob("holdout")), [])
            self.assertFalse((run_data_dir(tenant) / "holdout").exists())
            bodies = []
            for cand in res["candidates"]:
                self.assertTrue(Path(cand["design_dir"]).is_dir())
                self.assertNotIn("spec_path", cand)
                task = _get_task(cand["task_id"])
                self.assertEqual(task.title, f"[design] [arena:{cand['vendor']}] Retry policy")
                self.assertIn("factonio:design", task.skills)
                self.assertIn(f"{cand['design_dir']}/DESIGN.md", task.body)
                bodies.append(task.body)
            judge = _get_task(res["judge_task_id"])
            self.assertIn("factonio:design", judge.skills)
            self.assertIn(str(design_dir_path(tenant)), judge.body)
            bodies.append(judge.body)
            for body in bodies:
                self.assertNotIn("/holdout", body)
                self.assertIn("Do not search for or read any `holdout/` files.", body)
            self.assertEqual(res["canonical"]["design_dir"], str(design_dir_path(tenant)))
            self.assertNotIn("holdout_dir", res["canonical"])

    def test_design_judge_emits_multi_variant_preview_switcher(self):
        from factonio.orchestrator import design_dir_path

        with isolated_hermes_home() as home:
            _register(home)
            res = _spawn(target="design")
            design_dir = design_dir_path(res["tenant"])
            body = _get_task(res["judge_task_id"]).body
            synthesis = body.split("## Hand-Grafted Synthesis", 1)[1]
            for needle in (f"`{design_dir}/preview.html`", f"`{design_dir}/DESIGN.md`", "self-contained zero-build static HTML variant switcher",
                           "no external assets, CDN, or build step", "Candidate A / Candidate B", "Synthesis",
                           "side-by-side comparison", "alongside `DESIGN.md`"):
                self.assertIn(needle, synthesis)
            self.assertLess(synthesis.index("`## Arena Synthesis`"), synthesis.index("preview.html"))
            self.assertLess(synthesis.index("preview.html"), synthesis.index("factonio_log_decision"))
            self.assertNotIn("/holdout", body)
            self.assertIn("Do not search for or read any `holdout/` files.", body)

    def test_define_judge_has_no_preview_switcher(self):
        with isolated_hermes_home() as home:
            _register(home)
            res = _spawn()
            body = _get_task(res["judge_task_id"]).body
            self.assertIn("## Hand-Grafted Synthesis", body)
            for needle in ("preview.html", "variant switcher", "side-by-side"):
                self.assertNotIn(needle, body)


class TestEvaluateArena(unittest.TestCase):
    def test_lifecycle_is_read_only_until_synthesized(self):
        from factonio.arena import evaluate_arena

        with isolated_hermes_home() as home:
            _register(home)
            res = _spawn()
            aid = res["arena_id"]
            before = (_db_fingerprint(home), _task_count())
            ev = evaluate_arena(arena_id=aid)
            self.assertEqual((_db_fingerprint(home), _task_count()), before)
            self.assertTrue(ev["ok"])
            self.assertEqual(ev["status"], "candidates_in_progress")
            self.assertEqual(ev["judge"]["task_id"], res["judge_task_id"])
            self.assertFalse(ev["ready_for_synthesis"] or ev["synthesized"] or ev["canonical_artifact_exists"])
            self.assertLessEqual({"ok", "arena_id", "target", "status", "candidates", "judge", "ready_for_synthesis",
                                  "synthesized", "canonical_artifact_exists", "advisories"}, set(ev))

            c1, c2 = res["candidates"]
            Path(c1["spec_path"]).write_text("# Spec A\n", encoding="utf-8")
            (Path(c1["holdout_dir"]) / "s1.md").write_text("scenario", encoding="utf-8")
            _complete(c1["task_id"], None)
            _complete(c2["task_id"], None)
            ev = evaluate_arena(arena_id=aid)
            self.assertEqual(ev["status"], "needs_attention")
            self.assertFalse(ev["ready_for_synthesis"])
            self.assertTrue(any("c2-openai" in a and "missing or empty" in a for a in ev["advisories"]))
            self.assertEqual(ev["candidates"][0]["holdout_files"], 1)
            self.assertEqual(ev["candidates"][0]["artifact"]["size_bytes"], len("# Spec A\n"))

            Path(c2["spec_path"]).write_text("# Spec B\n", encoding="utf-8")
            ev = evaluate_arena(tenant=res["tenant"])
            self.assertEqual((ev["status"], ev["ready_for_synthesis"]), ("judging", True))

            _complete(res["judge_task_id"], None)
            ev = evaluate_arena(arena_id=aid)
            self.assertEqual(ev["status"], "needs_attention")
            self.assertFalse(ev["synthesized"])
            Path(res["canonical"]["spec_path"]).write_text("# Canonical\n", encoding="utf-8")
            ev = evaluate_arena(arena_id=aid)
            self.assertEqual(ev["status"], "synthesized")
            self.assertTrue(ev["synthesized"] and ev["canonical_artifact_exists"])

    def test_spawn_judge_on_demand_once(self):
        from factonio.arena import evaluate_arena

        with isolated_hermes_home() as home:
            _register(home)
            res = _spawn(auto_judge=False)
            self.assertIsNone(res["judge_task_id"])
            self.assertEqual(_task_count(), 2)
            ev = evaluate_arena(arena_id=res["arena_id"])
            self.assertIsNone(ev["judge"])
            for cand in res["candidates"]:
                Path(cand["spec_path"]).write_text("spec", encoding="utf-8")
                _complete(cand["task_id"], None)
            self.assertEqual(evaluate_arena(arena_id=res["arena_id"])["status"], "ready_for_synthesis")

            with self.assertRaisesRegex(ValueError, "shares vendor family anthropic"):
                evaluate_arena(arena_id=res["arena_id"], spawn_judge=True, judge_model="anthropic/claude-haiku-4-5",
                               strict_cross_vendor=True)
            self.assertEqual(_task_count(), 2)

            ev = evaluate_arena(arena_id=res["arena_id"], spawn_judge=True, judge_model=GOOGLE_JUDGE)
            self.assertTrue(ev["spawned_judge"])
            judge_id = ev["judge"]["task_id"]
            self.assertEqual(ev["status"], "judging")
            self.assertEqual(_parents(judge_id), sorted(res["candidate_task_ids"]))
            self.assertIn("Design the webhook retry policy.", _get_task(judge_id).body)
            manifest = json.loads(Path(res["manifest_path"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["judge"]["task_id"], judge_id)
            self.assertEqual(len(_trail_rows("spawn_arena_judge", res["tenant"])), 1)

            again = evaluate_arena(arena_id=res["arena_id"], spawn_judge=True)
            self.assertFalse(again["spawned_judge"])
            self.assertEqual(again["judge"]["task_id"], judge_id)
            self.assertTrue(any("already exists" in a for a in again["advisories"]))
            self.assertEqual(_task_count(), 3)

    def test_unknown_or_invalid_arena_and_missing_kanban_db_never_created(self):
        from factonio.arena import evaluate_arena

        with isolated_hermes_home() as home:
            _register(home)
            self.assertFalse(evaluate_arena(arena_id="../../etc")["ok"])
            self.assertFalse(evaluate_arena(arena_id="ar_ffffffffff")["ok"])
            self.assertFalse(evaluate_arena(tenant="nobody")["ok"])
            self.assertFalse(evaluate_arena()["ok"])

            res = _spawn()
            missing = home / "elsewhere" / "kanban.db"
            os.environ["HERMES_KANBAN_DB"] = str(missing)
            try:
                ev = evaluate_arena(arena_id=res["arena_id"])
            finally:
                os.environ["HERMES_KANBAN_DB"] = str(home / "kanban.db")
            self.assertTrue(ev["ok"])
            self.assertEqual(ev["status"], "degraded")
            self.assertFalse(missing.exists())
            self.assertFalse(missing.parent.exists())


def _set_status(task_id: str, status: str) -> None:
    from hermes_cli.kanban_db_connect import connect_closing

    with connect_closing() as conn:
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))


def _manifest(res: dict[str, Any]) -> dict[str, Any]:
    return json.loads(Path(res["manifest_path"]).read_text(encoding="utf-8"))


def _finish_candidates(res: dict[str, Any], *, write: bool = True) -> None:
    for cand in res["candidates"]:
        if write:
            path = Path(cand["spec_path"]) if "spec_path" in cand else Path(cand["design_dir"]) / "DESIGN.md"
            path.write_text(f"# {cand['slot']}\n", encoding="utf-8")
        _complete(cand["task_id"], None)


class TestArenaFixRound1(unittest.TestCase):
    def test_deferred_judge_reuses_saved_model_and_strict_policy(self):
        from factonio.arena import evaluate_arena

        with isolated_hermes_home() as home:
            _register(home)
            res = _spawn(auto_judge=False, strict_cross_vendor=True)
            ev = evaluate_arena(arena_id=res["arena_id"], spawn_judge=True)
            self.assertTrue(ev["spawned_judge"])
            task = _get_task(ev["judge"]["task_id"])
            self.assertEqual((task.provider_override, task.model_override), ("google", "gemini-2.5-pro"))
            manifest = _manifest(res)
            self.assertEqual(manifest["judge"]["label"], GOOGLE_JUDGE)
            self.assertEqual(manifest["judge"]["source"], "manifest")
            self.assertTrue(manifest["judge"]["cross_vendor"])
            self.assertTrue(manifest["strict_cross_vendor"])
            ev_row = _trail_rows("spawn_arena_judge", res["tenant"])[0]["evidence"]
            self.assertTrue(ev_row["strict_cross_vendor"])
            self.assertEqual(ev_row["judge_model"], GOOGLE_JUDGE)

    def test_deferred_judge_on_strict_arena_rejects_overlap_without_repeating_flag(self):
        from factonio.arena import evaluate_arena

        with isolated_hermes_home() as home:
            _register(home)
            res = _spawn(auto_judge=False, strict_cross_vendor=True)
            with self.assertRaisesRegex(ValueError, "shares vendor family anthropic"):
                evaluate_arena(arena_id=res["arena_id"], spawn_judge=True, judge_model="anthropic/claude-haiku-4-5")
            self.assertEqual(_task_count(), 2)
            self.assertEqual(_trail_rows("spawn_arena_judge", res["tenant"]), [])
            self.assertTrue(_manifest(res)["strict_cross_vendor"])
            self.assertEqual(_manifest(res)["judge"]["label"], GOOGLE_JUDGE)

    def test_unknown_vendor_candidate_is_never_cross_vendor(self):
        with isolated_hermes_home() as home:
            _register(home)
            models = TWO_VENDORS + ["mystery-model-x"]
            with self.assertRaisesRegex(ValueError, "could not be inferred"):
                _spawn(candidate_models=models, strict_cross_vendor=True)
            self.assertEqual((_task_count(), _arena_dirs(home)), (0, []))
            res = _spawn(candidate_models=models)
            self.assertFalse(res["judge"]["cross_vendor"])
            self.assertTrue(any("not provably cross-vendor" in a for a in res["advisories"]))

    def test_partial_spawn_failure_rolls_back_every_card_and_directory(self):
        from unittest import mock

        from hermes_cli import kanban_db as kb

        real = kb.create_task
        for fail_at in (2, 3):
            calls = {"n": 0}

            def flaky(*args: Any, **kwargs: Any) -> str:
                calls["n"] += 1
                if calls["n"] == fail_at:
                    raise sqlite3.OperationalError("disk I/O error")
                return real(*args, **kwargs)

            with self.subTest(fail_at=fail_at), isolated_hermes_home() as home:
                _register(home)
                with mock.patch.object(kb, "create_task", side_effect=flaky):
                    with self.assertRaises(sqlite3.OperationalError):
                        _spawn()
                self.assertEqual(_task_count(), 0)
                self.assertEqual(_arena_dirs(home), [])
                self.assertEqual(_trail_rows("spawn_arena"), [])

    def test_manifest_write_failure_rolls_back_cards(self):
        from unittest import mock

        from factonio import arena

        with isolated_hermes_home() as home:
            _register(home)
            with mock.patch.object(arena, "_write_json_atomic", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    _spawn()
            self.assertEqual((_task_count(), _arena_dirs(home)), (0, []))

    def test_no_false_synthesis_with_unfinished_or_missing_candidate_artifacts(self):
        from factonio.arena import evaluate_arena

        with isolated_hermes_home() as home:
            _register(home)
            res = _spawn()
            Path(res["canonical"]["spec_path"]).write_text("# Canonical\n", encoding="utf-8")
            _set_status(res["judge_task_id"], "done")
            ev = evaluate_arena(arena_id=res["arena_id"])
            self.assertFalse(ev["synthesized"])
            self.assertEqual(ev["status"], "needs_attention")
            self.assertTrue(any("not every candidate finished" in a for a in ev["advisories"]))

            _finish_candidates(res, write=False)
            ev = evaluate_arena(arena_id=res["arena_id"])
            self.assertFalse(ev["synthesized"])
            self.assertEqual(ev["status"], "needs_attention")

            for cand in res["candidates"]:
                Path(cand["spec_path"]).write_text("spec", encoding="utf-8")
            ev = evaluate_arena(arena_id=res["arena_id"])
            self.assertEqual((ev["status"], ev["synthesized"]), ("synthesized", True))

    def test_stale_pre_existing_canonical_artifact_is_not_synthesized(self):
        from factonio.arena import evaluate_arena
        from factonio.orchestrator import design_dir_path, spec_file_path

        for target in ("define", "design"):
            with self.subTest(target=target), isolated_hermes_home() as home:
                _register(home)
                tenant = f"acme-svc-stale-{target}"
                canonical = spec_file_path(tenant) if target == "define" else design_dir_path(tenant) / "DESIGN.md"
                canonical.parent.mkdir(parents=True, exist_ok=True)
                canonical.write_text("# Old canonical\n", encoding="utf-8")
                old = canonical.stat().st_mtime_ns - 3600 * 1_000_000_000
                os.utime(canonical, ns=(old, old))

                res = _spawn(target=target, tenant=tenant)
                baseline = _manifest(res)["canonical_baseline"]
                self.assertTrue(baseline["existed"])
                self.assertEqual(baseline["sha256"], hashlib.sha256(b"# Old canonical\n").hexdigest())
                _finish_candidates(res)
                _complete(res["judge_task_id"], None)
                ev = evaluate_arena(arena_id=res["arena_id"])
                self.assertFalse(ev["synthesized"])
                self.assertEqual(ev["status"], "needs_attention")
                self.assertTrue(any("unchanged since before the arena" in a for a in ev["advisories"]))

                canonical.write_text("# Grafted canonical\n", encoding="utf-8")
                ev = evaluate_arena(arena_id=res["arena_id"])
                self.assertEqual((ev["status"], ev["synthesized"]), ("synthesized", True))

    def test_archived_failed_or_cancelled_candidates_are_terminal(self):
        from factonio.arena import evaluate_arena

        for state, expected in (("archived", "degraded"), ("failed", "needs_attention"), ("cancelled", "needs_attention")):
            with self.subTest(state=state), isolated_hermes_home() as home:
                _register(home)
                res = _spawn(auto_judge=False)
                c1, c2 = res["candidates"]
                Path(c1["spec_path"]).write_text("spec", encoding="utf-8")
                _complete(c1["task_id"], None)
                _set_status(c2["task_id"], state)
                ev = evaluate_arena(arena_id=res["arena_id"])
                self.assertEqual(ev["status"], expected)
                self.assertFalse(ev["ready_for_synthesis"])
                self.assertTrue(any(c2["slot"] in a and state in a for a in ev["advisories"]))

                ev = evaluate_arena(arena_id=res["arena_id"], spawn_judge=True)
                self.assertFalse(ev["spawned_judge"])
                self.assertIsNone(ev["judge"])
                self.assertTrue(any("Judge not spawned" in a for a in ev["advisories"]))
                self.assertEqual(_task_count(), 2)

    def test_spawn_judge_reports_actual_ready_status(self):
        from factonio.arena import evaluate_arena

        with isolated_hermes_home() as home:
            _register(home)
            res = _spawn(auto_judge=False)
            _finish_candidates(res)
            ev = evaluate_arena(arena_id=res["arena_id"], spawn_judge=True)
            self.assertTrue(ev["spawned_judge"])
            self.assertEqual(ev["judge"]["status"], "ready")
            self.assertEqual(_get_task(ev["judge"]["task_id"]).status, "ready")

    def test_same_second_tenant_lookup_is_deterministic(self):
        from factonio.arena import evaluate_arena

        with isolated_hermes_home() as home:
            _register(home)
            first = _spawn(auto_judge=False)
            second = _spawn(auto_judge=False)
            for res, ns in ((first, 2_000_000_000_500), (second, 2_000_000_000_100)):
                manifest = _manifest(res)
                manifest.update(created_at=2000, created_at_ns=ns)
                Path(res["manifest_path"]).write_text(json.dumps(manifest), encoding="utf-8")
            os.utime(first["manifest_path"], ns=(1, 1))
            for _ in range(3):
                self.assertEqual(evaluate_arena(tenant="acme-svc-arena")["arena_id"], first["arena_id"])
            self.assertGreater(_manifest(second)["created_at_ns"], 0)

    def test_auto_provider_is_stripped_from_judge(self):
        with isolated_hermes_home() as home:
            _register(home)
            _set_repo_field("acme-svc", models={"define": {"provider": "auto", "model": "gemini-2.5-pro"}})
            for judge_model in (None, "auto/gemini-2.5-pro", {"provider": "AUTO", "model": "gemini-2.5-pro"}):
                with self.subTest(judge_model=judge_model):
                    res = _spawn(tenant=None, judge_model=judge_model)
                    self.assertIsNone(res["judge"]["provider"])
                    self.assertEqual(res["judge"]["label"], "gemini-2.5-pro")
                    task = _get_task(res["judge_task_id"])
                    self.assertIsNone(task.provider_override)
                    self.assertEqual(task.model_override, "gemini-2.5-pro")

    def test_design_judge_body_names_hermes_provider(self):
        with isolated_hermes_home() as home:
            _register(home)
            res = _spawn(target="design")
            self.assertIn("- Design provider: `hermes` (pure-Hermes synthesis on this judge's own model)",
                          _get_task(res["judge_task_id"]).body)


class TestArenaFixRound2(unittest.TestCase):
    def _assert_no_deferred_judge(self, res: dict[str, Any], before: bytes) -> None:
        self.assertEqual(_task_count(), 2)
        self.assertEqual(Path(res["manifest_path"]).read_bytes(), before)
        self.assertEqual(_trail_rows("spawn_arena_judge"), [])

    def _assert_retry_spawns_full_judge(self, res: dict[str, Any]) -> None:
        from factonio.arena import evaluate_arena

        ev = evaluate_arena(arena_id=res["arena_id"], spawn_judge=True, judge_model=GOOGLE_JUDGE,
                            strict_cross_vendor=True)
        self.assertTrue(ev["spawned_judge"], ev)
        self.assertEqual((ev["judge"]["vendor"], ev["judge"]["cross_vendor"]), ("google", True))
        self.assertEqual(_task_count(), 3)
        manifest = _manifest(res)
        self.assertEqual(manifest["judge"]["task_id"], ev["judge"]["task_id"])
        self.assertEqual(manifest["judge"]["label"], GOOGLE_JUDGE)
        self.assertTrue(manifest["strict_cross_vendor"])
        rows = _trail_rows("spawn_arena_judge", res["tenant"])
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["evidence"]["strict_cross_vendor"])

    def test_deferred_judge_manifest_write_failure_rolls_back_the_judge_card(self):
        from unittest import mock

        from factonio import arena
        from factonio.arena import evaluate_arena

        with isolated_hermes_home() as home:
            _register(home)
            res = _spawn(auto_judge=False, judge_model="vertex/gemini-2.5-flash")
            _finish_candidates(res)
            before = Path(res["manifest_path"]).read_bytes()
            with mock.patch.object(arena, "_write_json_atomic", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    evaluate_arena(arena_id=res["arena_id"], spawn_judge=True, judge_model=GOOGLE_JUDGE,
                                   strict_cross_vendor=True)
            self._assert_no_deferred_judge(res, before)
            self.assertIsNone(evaluate_arena(arena_id=res["arena_id"])["judge"])
            self._assert_retry_spawns_full_judge(res)

    def test_deferred_judge_read_only_arena_dir_rolls_back_and_cli_reports_json_error(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        with isolated_hermes_home() as home:
            _register(home)
            res = _spawn(auto_judge=False)
            _finish_candidates(res)
            arena_dir = Path(res["arena_dir"])
            before = Path(res["manifest_path"]).read_bytes()
            run = _cli()
            arena_dir.chmod(0o500)
            try:
                code, out, err = run("arena", "status", "--arena-id", res["arena_id"], "--spawn-judge",
                                     "--judge-model", GOOGLE_JUDGE, "--strict-cross-vendor", "--json")
            finally:
                arena_dir.chmod(0o700)
            self.assertEqual(code, 1)
            payload = json.loads(out)
            self.assertFalse(payload["ok"])
            self.assertIn("Permission denied", payload["error"])
            self.assertNotIn("Traceback", err)
            self._assert_no_deferred_judge(res, before)
            self._assert_retry_spawns_full_judge(res)

    def test_failed_or_cancelled_judge_needs_attention(self):
        from factonio.arena import evaluate_arena

        for state in ("failed", "cancelled"):
            with self.subTest(state=state), isolated_hermes_home() as home:
                _register(home)
                res = _spawn()
                _finish_candidates(res)
                _set_status(res["judge_task_id"], state)
                ev = evaluate_arena(arena_id=res["arena_id"])
                self.assertEqual(ev["status"], "needs_attention")
                self.assertFalse(ev["synthesized"])
                self.assertTrue(any(res["judge_task_id"] in a and f"'{state}'" in a for a in ev["advisories"]),
                                ev["advisories"])

    def test_unreadable_canonical_artifact_fails_closed(self):
        from unittest import mock

        from factonio import arena
        from factonio.arena import _canonical_changed, evaluate_arena
        from factonio.orchestrator import spec_file_path

        with isolated_hermes_home() as home:
            _register(home)
            tenant = "acme-svc-unreadable"
            canonical = spec_file_path(tenant)
            canonical.parent.mkdir(parents=True, exist_ok=True)
            canonical.write_text("# Old canonical\n", encoding="utf-8")
            res = _spawn(tenant=tenant)
            _finish_candidates(res)
            _complete(res["judge_task_id"], None)
            # A newer mtime alone must not count as synthesis when the content cannot be read.
            future = canonical.stat().st_mtime_ns + 3600 * 1_000_000_000
            os.utime(canonical, ns=(future, future))
            manifest = _manifest(res)

            with mock.patch.object(arena, "_sha256", return_value=None):
                self.assertFalse(_canonical_changed(canonical, manifest))
                self.assertFalse(evaluate_arena(arena_id=res["arena_id"])["synthesized"])

            if not (hasattr(os, "geteuid") and os.geteuid() == 0):
                canonical.chmod(0o000)
                try:
                    self.assertFalse(_canonical_changed(canonical, manifest))
                    ev = evaluate_arena(arena_id=res["arena_id"])
                finally:
                    canonical.chmod(0o600)
                self.assertFalse(ev["synthesized"])
                self.assertEqual(ev["status"], "needs_attention")


def _summary_view(ev: dict[str, Any]) -> dict[str, Any]:
    """The Dashboard-safe projection of an `evaluate_arena` result."""
    return {
        "status": ev["status"],
        "ready_for_synthesis": ev["ready_for_synthesis"],
        "synthesized": ev["synthesized"],
        "canonical_artifact_exists": ev["canonical_artifact_exists"],
        "judge": ev["judge"],
        "candidates": [{k: c[k] for k in ("slot", "vendor", "model", "task_id", "status", "artifact_ready")}
                       for c in ev["candidates"]],
    }


def _assert_barrier(case: unittest.TestCase, payload: Any, home: Path) -> None:
    text = json.dumps(payload)
    case.assertNotIn("holdout", text.lower())
    case.assertNotIn(str(home), text)
    for key in ("holdout_files", "holdout_dir", "manifest_path", "artifact", "canonical_artifact", "path"):
        case.assertNotIn(f'"{key}"', text)


class TestArenaDashboardSummaries(unittest.TestCase):
    def test_summaries_share_evaluate_state_machine_and_are_read_only(self):
        from factonio.arena import evaluate_arena, list_arena_summaries
        from factonio.metrics import thread_cards

        with isolated_hermes_home() as home:
            _register(home)
            res = _spawn()
            tenant, aid = res["tenant"], res["arena_id"]
            c1, c2 = res["candidates"]
            (Path(c1["holdout_dir"]) / "scenario-secret.md").write_text("hidden", encoding="utf-8")
            manifest_before = Path(res["manifest_path"]).read_bytes()

            def check(expected_status: str) -> dict[str, Any]:
                before = (_db_fingerprint(home), _task_count())
                summaries = list_arena_summaries(tenants=[tenant], db_path=home / "kanban.db")
                cards = thread_cards(home / "kanban.db", tenant=tenant)
                self.assertEqual((_db_fingerprint(home), _task_count()), before)
                self.assertEqual(Path(res["manifest_path"]).read_bytes(), manifest_before)
                self.assertEqual(len(summaries), 1)
                summary = summaries[0]
                self.assertEqual(cards["arenas"], summaries)
                ev = evaluate_arena(arena_id=aid)
                self.assertEqual(summary["status"], expected_status)
                self.assertEqual({k: summary[k] for k in _summary_view(ev)}, _summary_view(ev))
                self.assertEqual(
                    {k: summary[k] for k in ("arena_id", "tenant", "repo", "target", "title")},
                    {"arena_id": aid, "tenant": tenant, "repo": "acme/svc", "target": "define", "title": "Retry policy"},
                )
                self.assertIsInstance(summary["created_at"], int)
                _assert_barrier(self, cards["arenas"], home)
                return cards

            cards = check("candidates_in_progress")
            by_id = {c["id"]: c for c in cards["cards"]}
            self.assertEqual(
                {k: by_id[c1["task_id"]][k] for k in ("arena_id", "arena_role", "arena_slot", "arena_vendor", "arena_target")},
                {"arena_id": aid, "arena_role": "candidate", "arena_slot": "c1-anthropic", "arena_vendor": "anthropic",
                 "arena_target": "define"},
            )
            judge = by_id[res["judge_task_id"]]
            self.assertEqual((judge["arena_role"], judge["arena_slot"], judge["arena_vendor"]), ("judge", None, "google"))

            Path(c1["spec_path"]).write_text("# A\n", encoding="utf-8")
            _complete(c1["task_id"], None)
            _complete(c2["task_id"], None)
            summary = check("needs_attention")["arenas"][0]
            self.assertTrue(any("c2-openai" in a and "missing or empty" in a for a in summary["advisories"]))
            Path(c2["spec_path"]).write_text("# B\n", encoding="utf-8")
            check("judging")
            _complete(res["judge_task_id"], None)
            summary = check("needs_attention")["arenas"][0]
            self.assertTrue(any("canonical artifact is missing" in a for a in summary["advisories"]))
            Path(res["canonical"]["spec_path"]).write_text("# Canonical\n", encoding="utf-8")
            summary = check("synthesized")["arenas"][0]
            self.assertTrue(summary["synthesized"] and summary["canonical_artifact_exists"])

    def test_single_ro_query_newest_first_and_corrupt_manifests_skipped(self):
        import sqlite3 as sqlite_mod
        from unittest import mock

        from factonio import arena
        from factonio.arena import list_arena_summaries

        with isolated_hermes_home() as home:
            _register(home)
            first = _spawn(target="define")
            second = _spawn(target="design")
            arena_root = _arena_root(home) / "acme-svc-arena" / "arena"
            (arena_root / "ar_deadbeef00").mkdir()
            (arena_root / "ar_deadbeef00" / "manifest.json").write_text("{not json", encoding="utf-8")
            (arena_root / "ar_abcdef1234").mkdir()
            (arena_root / "ar_abcdef1234" / "manifest.json").write_text(json.dumps({"arena_id": "ar_0000000000"}),
                                                                        encoding="utf-8")
            (arena_root / "ar_aaaaaaaaaa").mkdir()
            (arena_root / "ar_aaaaaaaaaa" / "manifest.json").write_text(
                json.dumps({"arena_id": "ar_aaaaaaaaaa", "candidates": 5, "judge": 7}), encoding="utf-8")
            (arena_root / "not-an-arena").mkdir()

            real_connect = sqlite_mod.connect
            calls: list[str] = []

            def counting(target: Any, *a: Any, **k: Any):
                calls.append(str(target))
                return real_connect(target, *a, **k)

            with mock.patch.object(arena.sqlite3, "connect", counting):
                out = list_arena_summaries(tenants=["acme-svc-arena", "acme-svc-arena", "", "nobody"],
                                           db_path=home / "kanban.db")
            self.assertEqual(len(calls), 1)
            self.assertTrue(calls[0].endswith("?mode=ro"))
            ids = [s["arena_id"] for s in out]
            self.assertEqual(ids[:2], [second["arena_id"], first["arena_id"]])
            self.assertNotIn("ar_deadbeef00", ids)
            self.assertNotIn("ar_abcdef1234", ids)
            self.assertNotIn("ar_0000000000", ids)
            self.assertEqual(out[0]["target"], "design")
            _assert_barrier(self, out, home)

            self.assertEqual(list_arena_summaries(tenants=[]), [])
            self.assertEqual(list_arena_summaries(tenants=["nobody"]), [])
            self.assertFalse((_arena_root(home) / "nobody").exists())

    def test_missing_kanban_db_and_holdout_strings_fail_closed(self):
        from factonio.arena import list_arena_summaries

        with isolated_hermes_home() as home:
            arena_dir = _arena_root(home) / "ws-h" / "arena" / "ar_1234567890"
            arena_dir.mkdir(parents=True)
            (arena_dir / "manifest.json").write_text(json.dumps({
                "arena_id": "ar_1234567890",
                "tenant": "ws-h",
                "target": "define",
                "title": "Copy holdout/scenario-1.md into spec",
                "created_at_ns": 1,
                "candidates": [
                    {"slot": "c1-holdout-reader", "vendor": "anthropic", "task_id": "t_gone",
                     "spec_path": str(arena_dir / "candidates" / "c1" / "spec.md"),
                     "holdout_dir": str(arena_dir / "candidates" / "c1" / "holdout")},
                    {"slot": "c2-openai", "vendor": "openai", "task_id": "t_gone2"},
                ],
                "judge": {"task_id": "t_judge", "vendor": "google"},
            }), encoding="utf-8")
            missing = home / "elsewhere" / "kanban.db"
            out = list_arena_summaries(tenants=["ws-h"], db_path=missing)
            self.assertFalse(missing.parent.exists())
            self.assertEqual(len(out), 1)
            self.assertEqual(out[0]["status"], "degraded")
            self.assertIsNone(out[0]["title"])
            self.assertIn("kanban.db not found", out[0]["advisories"])
            self.assertTrue(any("c2-openai" in a for a in out[0]["advisories"]))
            self.assertFalse(any("c1-holdout" in a for a in out[0]["advisories"]))
            self.assertIsNone(out[0]["candidates"][0]["slot"])
            _assert_barrier(self, out, home)


class TestArenaToolsAndCli(unittest.TestCase):
    def test_tools(self):
        with isolated_hermes_home() as home:
            _register(home)
            tools = _tools()
            manifest = (PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8")
            for name in ("factonio_spawn_arena", "factonio_evaluate_arena"):
                self.assertIn(name, tools)
                self.assertIn(f"  - {name}\n", manifest)
            schema = tools["factonio_spawn_arena"]["schema"]["parameters"]
            self.assertEqual(schema["properties"]["target"]["enum"], ["define", "design"])

            bad = json.loads(tools["factonio_spawn_arena"]["handler"](
                {"repo": "acme/svc", "request": "x", "candidate_models": SAME_VENDOR}))
            self.assertFalse(bad["ok"])
            self.assertIn("vendor families", bad["error"])
            self.assertEqual(_task_count(), 0)

            ok = json.loads(tools["factonio_spawn_arena"]["handler"]({
                "repo": "acme/svc", "tenant": "acme-svc-tool", "request": "Pick a cache design", "target": "design",
                "candidate_models": TWO_VENDORS, "judge_model": GOOGLE_JUDGE, "strict_cross_vendor": True,
            }))
            self.assertTrue(ok["ok"], ok)
            self.assertEqual(ok["target"], "design")
            ev = json.loads(tools["factonio_evaluate_arena"]["handler"]({"tenant": "acme-svc-tool"}))
            self.assertEqual((ev["ok"], ev["arena_id"]), (True, ok["arena_id"]))

    def test_cli_spawn_and_status(self):
        with isolated_hermes_home() as home:
            _register(home)
            run = _cli()
            code, out, _ = run("arena", "spawn", "acme/svc", "--request", "Contested spec", "--candidate", SAME_VENDOR[0],
                               "--candidate", SAME_VENDOR[1], "--json")
            self.assertEqual(code, 1)
            self.assertFalse(json.loads(out)["ok"])
            self.assertEqual(_task_count(), 0)

            code, out, _ = run("arena", "spawn", "acme/svc", "--request", "Contested spec", "--tenant", "acme-svc-cli",
                               "--candidate", TWO_VENDORS[0], "--candidate", TWO_VENDORS[1],
                               "--judge-model", GOOGLE_JUDGE, "--strict-cross-vendor", "--no-judge", "--json")
            self.assertEqual(code, 0, out)
            spawned = json.loads(out)
            self.assertEqual((spawned["action"], spawned["judge_task_id"]), ("spawn", None))

            code, out, _ = run("arena", "status", "--arena-id", spawned["arena_id"])
            self.assertEqual(code, 0)
            self.assertIn("CANDIDATES_IN_PROGRESS", out)
            code, out, _ = run("arena", "status", "--tenant", "acme-svc-cli", "--spawn-judge", "--judge-model",
                               GOOGLE_JUDGE, "--json")
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(out)["spawned_judge"])
            self.assertEqual(_task_count(), 3)

            code, _, err = run("arena", "status", "--arena-id", "ar_0000000000")
            self.assertEqual(code, 1)
            self.assertIn("No arena found", err)
            code, _, err = run("arena", "spawn", "acme/svc")
            self.assertEqual(code, 1)
            self.assertIn("--request", err)


class TestArenaDocs(unittest.TestCase):
    def test_orchestrator_skill_section_1b_between_1_and_2(self):
        text = (PLUGIN_ROOT / "skills" / "orchestrator" / "SKILL.md").read_text(encoding="utf-8")
        heading = "### 1b. Optional Arena Mode (Contested Specs or Designs)"
        self.assertLess(text.index("### 1. Intake and Clarification"), text.index(heading))
        self.assertLess(text.index(heading), text.index("### 2. Optional Phase 1b Design (UI Work)"))
        section = text.split(heading, 1)[1].split("\n### ", 1)[0]
        for needle in ("factonio_spawn_arena", "factonio_evaluate_arena", "strict_cross_vendor", "arena_synthesis",
                       "holdout/"):
            self.assertIn(needle, section)

    def test_prompt_section_stays_under_budget(self):
        import factonio

        self.assertLess(len(factonio.ORCHESTRATOR_PROMPT_SECTION), 2900)
        self.assertIn("factonio_spawn_arena", factonio.ORCHESTRATOR_PROMPT_SECTION)


if __name__ == "__main__":
    unittest.main()
