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
