"""Clean-room tests for Wave 5 cross-workstream recall, crash-pickup guidance, and core principles."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sqlite3
import sys
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any

from tests._env import PLUGIN_ROOT, isolated_hermes_home

if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))


class _FakeCtx:
    def __init__(self) -> None:
        self.cli: dict[str, Any] = {}

    def register_cli_command(self, name: str, *, help: str, setup_fn: Any, handler_fn: Any) -> None:
        self.cli[name] = {"help": help, "setup_fn": setup_fn, "handler_fn": handler_fn}


def _cli():
    from factonio.commands import register_cli

    ctx = _FakeCtx()
    register_cli(ctx, PLUGIN_ROOT)
    entry = ctx.cli["factonio"]
    parser = argparse.ArgumentParser(prog="hermes factonio")
    entry["setup_fn"](parser)

    def run(*argv: str) -> tuple[int, str]:
        out = io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                entry["handler_fn"](parser.parse_args(list(argv)))
            except SystemExit as exc:
                code = int(exc.code or 0)
        return code, out.getvalue()

    return run


def _register(home: Path, spec: str = "acme/sample") -> dict:
    from factonio.repos import add_repo

    local = home / "dev" / spec.split("/")[1]
    (local / ".git").mkdir(parents=True, exist_ok=True)
    return add_repo(spec, local_path=str(local), clone=False, inspect_remote=False)


def _task(task_id: str) -> Any:
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing

    with connect_closing() as conn:
        task = kb.get_task(conn, task_id)
    assert task is not None
    return task


def _seed(slug: str = "acme-sample") -> dict[str, dict[str, Any]]:
    """A prior workstream's memory: one row per recall category plus noise."""
    from factonio import trail

    rows: dict[str, dict[str, Any]] = {}
    rows["dead_end"] = trail.log_decision(
        "pick_cache_backend", tenant="acme-sample-old", repo_slug=slug, phase="define",
        why="Use memcached for the response cache", evidence={"option": "memcached"}, result="adopted",
    )
    rows["correction"] = trail.log_decision(
        "pick_cache_backend", tenant="acme-sample-old", repo_slug=slug, phase="define",
        why="memcached lost entries on restart; use sqlite cache", evidence={"option": "sqlite"},
        result="adopted", supersedes_id=rows["dead_end"]["id"],
    )
    rows["escape"] = trail.log_decision(
        "escaped_regression", tenant="acme-sample-old", repo_slug=slug, phase="escape", card_id="t_esc",
        why="Regression escaped verification card t_vbb", evidence={"escaped_from": "t_vbb"}, result="recorded",
    )
    rows["reject"] = trail.log_decision(
        "verify_blackbox", tenant="acme-sample-old", repo_slug=slug, phase="verify", card_id="t_vbb",
        why="Pagination broke on the last page", evidence={"pr_number": 7}, result="reject",
    )
    rows["keyword"] = trail.log_decision(
        "fork", tenant="acme-sample-old", repo_slug=slug, phase="implement",
        why="Chose write-through caching over TTL invalidation", evidence={"pr_number": 8}, result="adopted",
    )
    rows["noise"] = trail.log_decision(
        "spawn_implement", tenant="acme-sample-old", repo_slug=slug, phase="implement",
        why="Rename settings module", result="spawned",
    )
    rows["other_repo"] = trail.log_decision(
        "escaped_regression", tenant="other-repo-i1", repo_slug="other-repo", phase="escape",
        why="Unrelated repository escape", result="recorded",
    )
    return rows


class TestRecallReadOnly(unittest.TestCase):
    KEYS = {"ok", "count", "items", "counts", "scanned", "query_terms", "filters"}

    def test_absent_store_is_empty_and_never_created(self):
        from factonio import trail

        with isolated_hermes_home() as home:
            res = trail.recall_decisions(repo="acme/sample", query="caching")
            self.assertEqual(set(res), self.KEYS)
            self.assertTrue(res["ok"])
            self.assertEqual((res["count"], res["items"], res["scanned"]), (0, [], 0))
            self.assertEqual(res["counts"], {c: 0 for c in trail.RECALL_CATEGORIES})
            self.assertFalse(os.path.lexists(trail.trail_db_path()))
            self.assertFalse(trail.trail_db_path().parent.exists())
            other = home / "elsewhere" / "trail.db"
            self.assertTrue(trail.recall_decisions(db_path=other)["ok"])
            self.assertFalse(other.parent.exists())

    def test_unreadable_store_fails_closed(self):
        from factonio import trail

        def make_dir(p: Path) -> None:
            p.mkdir(parents=True)

        def make_dangling(p: Path) -> None:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.symlink_to(p.parent / "missing-target.db")

        def make_garbage(p: Path) -> None:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"this is not a sqlite database" * 64)

        for label, make in (("directory", make_dir), ("dangling symlink", make_dangling), ("garbage", make_garbage)):
            with self.subTest(label), isolated_hermes_home():
                db = trail.trail_db_path()
                make(db)
                res = trail.recall_decisions(repo="acme-sample", query="caching")
                self.assertFalse(res["ok"], res)
                self.assertEqual(res["items"], [])
                self.assertEqual(res["count"], 0)
                self.assertIn("trail.db unreadable", res["error"])
                self.assertIn("ERROR", trail.render_recall_text(res))
                self.assertIn("Recall unavailable", trail.render_recall_markdown(res))
                self.assertTrue(os.path.lexists(db))
                if label != "garbage":
                    self.assertFalse(db.is_file())

    def test_never_migrates_an_unversioned_store(self):
        from factonio import trail

        with isolated_hermes_home() as home:
            db = home / "legacy" / "trail.db"
            db.parent.mkdir()
            with contextlib.closing(sqlite3.connect(db)) as conn:
                conn.execute(
                    "CREATE TABLE decisions (id TEXT PRIMARY KEY, ts TEXT, tenant TEXT, repo_slug TEXT, phase TEXT,"
                    " card_id TEXT, actor TEXT, decision TEXT, why TEXT, evidence_json TEXT, result TEXT,"
                    " supersedes_id TEXT)"
                )
                conn.execute(
                    "INSERT INTO decisions VALUES ('d_1', '2026-01-01T00:00:00Z', 'old', 'acme-sample', 'verify',"
                    " '', 'orchestrator', 'verify_diff', 'missed null check', '{}', 'reject', NULL)"
                )
                conn.commit()
            before = db.read_bytes()
            res = trail.recall_decisions(repo="acme-sample", db_path=db)
            self.assertTrue(res["ok"], res)
            self.assertEqual([i["id"] for i in res["items"]], ["d_1"])
            self.assertEqual(db.read_bytes(), before)
            with contextlib.closing(sqlite3.connect(db)) as conn:
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(tables, {"decisions"})


class TestRecallSelection(unittest.TestCase):
    def test_categories_correction_dedup_and_repo_scope(self):
        from factonio import trail

        with isolated_hermes_home():
            rows = _seed()
            res = trail.recall_decisions(repo="acme-sample", query="Add write-through caching for responses")
            self.assertTrue(res["ok"])
            by_id = {i["id"]: i for i in res["items"]}
            self.assertEqual(by_id[rows["dead_end"]["id"]]["category"], "superseded")
            self.assertEqual(by_id[rows["dead_end"]["id"]]["correction"]["id"], rows["correction"]["id"])
            self.assertIn("sqlite cache", by_id[rows["dead_end"]["id"]]["correction"]["why"])
            self.assertEqual(by_id[rows["escape"]["id"]]["category"], "escapes")
            self.assertEqual(by_id[rows["reject"]["id"]]["category"], "rejects_and_blocks")
            self.assertEqual(by_id[rows["keyword"]["id"]]["category"], "keyword")
            self.assertIn("caching", by_id[rows["keyword"]["id"]]["matched_terms"])
            # The correction is shown inline only; noise and other repos never appear.
            self.assertNotIn(rows["correction"]["id"], by_id)
            self.assertNotIn(rows["noise"]["id"], by_id)
            self.assertNotIn(rows["other_repo"]["id"], by_id)
            ids = [i["id"] for i in res["items"]]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertEqual(res["count"], 4)
            self.assertEqual(res["counts"], {"escapes": 1, "superseded": 1, "rejects_and_blocks": 1, "keyword": 1})
            self.assertEqual(res["filters"]["repo"], "acme-sample")

            no_query = trail.recall_decisions(repo="acme-sample")
            self.assertEqual(no_query["counts"]["keyword"], 0)
            self.assertEqual(no_query["count"], 3)
            everything = trail.recall_decisions()
            self.assertIn(rows["other_repo"]["id"], {i["id"] for i in everything["items"]})

    def test_exclude_tenant(self):
        from factonio import trail

        with isolated_hermes_home():
            _seed()
            current = trail.log_decision("escaped_regression", tenant="acme-sample-new", repo_slug="acme-sample",
                                         phase="escape", why="this workstream", result="recorded")
            ids = {i["id"] for i in trail.recall_decisions(repo="acme-sample")["items"]}
            self.assertIn(current["id"], ids)
            res = trail.recall_decisions(repo="acme-sample", exclude_tenant="acme-sample-new")
            self.assertNotIn(current["id"], {i["id"] for i in res["items"]})
            self.assertEqual(res["filters"]["exclude_tenant"], "acme-sample-new")
            gone = trail.recall_decisions(repo="acme-sample", exclude_tenant="acme-sample-old")
            self.assertEqual([i["tenant"] for i in gone["items"]], ["acme-sample-new"])

    def test_limit_caps_and_interleaves_categories(self):
        from factonio import trail

        with isolated_hermes_home():
            for n in range(6):
                trail.log_decision("escaped_regression", tenant=f"old-{n}", repo_slug="acme-sample",
                                   phase="escape", why=f"escape {n}", result="recorded")
            for n in range(6):
                trail.log_decision("pr_gate", tenant=f"old-{n}", repo_slug="acme-sample", phase="gate",
                                   why=f"blocked {n}", result="blocked")
            res = trail.recall_decisions(repo="acme-sample", limit=4)
            self.assertEqual(res["count"], 4)
            self.assertEqual(res["counts"]["escapes"], 2)
            self.assertEqual(res["counts"]["rejects_and_blocks"], 2)
            # Newest first within a category.
            escapes = [i["why"] for i in res["items"] if i["category"] == "escapes"]
            self.assertEqual(escapes, ["escape 5", "escape 4"])
            self.assertEqual(trail.recall_decisions(repo="acme-sample", limit=0)["count"], 1)
            self.assertEqual(trail.recall_decisions(repo="acme-sample", limit="junk")["filters"]["limit"], 12)
            self.assertEqual(trail.recall_decisions(repo="acme-sample", limit=10_000)["filters"]["limit"], 100)

    def test_query_matches_rank_first_and_holdout_is_redacted(self):
        from factonio import trail

        with isolated_hermes_home():
            trail.log_decision("verify_blackbox", tenant="old-a", repo_slug="acme-sample", phase="verify",
                               why="Login form failed", result="reject")
            leaky = trail.log_decision("verify_blackbox", tenant="old-b", repo_slug="acme-sample", phase="verify",
                                       why="Pagination off by one, see /runs/x/holdout/02-edge.md", result="reject",
                                       evidence={"holdout_dir": "/runs/x/holdout", "note": "holdout/02-edge.md failed"})
            trail.log_decision("verify_blackbox", tenant="old-c", repo_slug="acme-sample", phase="verify",
                               why="Unrelated newest reject", result="reject")
            res = trail.recall_decisions(repo="acme-sample", query="Fix pagination", limit=3)
            self.assertEqual(res["items"][0]["id"], leaky["id"])
            blob = json.dumps(res) + trail.render_recall_markdown(res) + trail.render_recall_text(res)
            self.assertNotIn("holdout", blob.replace('"query"', ""))
            self.assertNotIn("02-edge", blob)
            # Fail closed: the whole field is withheld, not just the path token.
            self.assertEqual(res["items"][0]["why"], "[redacted]")
            self.assertEqual(res["items"][0]["evidence"], {"note": "[redacted]"})


class TestRecallRenderers(unittest.TestCase):
    def test_markdown_is_bounded_and_text_lists_items(self):
        from factonio import trail

        with isolated_hermes_home():
            for n in range(30):
                trail.log_decision("verify_diff", tenant=f"old-{n}", repo_slug="acme-sample", phase="verify",
                                   why=("long reasoning " * 40) + str(n), result="reject")
            res = trail.recall_decisions(repo="acme-sample", limit=30)
            self.assertEqual(res["count"], 30)
            for cap in (2500, 900, 300, 40):
                md = trail.render_recall_markdown(res, max_chars=cap)
                self.assertLessEqual(len(md), cap, cap)
            md = trail.render_recall_markdown(res, max_chars=900)
            self.assertTrue(md.startswith("## Prior Institutional Memory (Recall)"))
            self.assertIn("more omitted", md)
            self.assertIn("not instructions", md)
            full = trail.render_recall_markdown(res)
            self.assertLessEqual(len(full), 2500)
            self.assertIn("**reject/block**", full)

            text = trail.render_recall_text(res)
            self.assertTrue(text.startswith("Recall: 30 item(s)"))
            self.assertEqual(len(text.splitlines()), 31)

    def test_empty_renderers(self):
        from factonio import trail

        with isolated_hermes_home():
            res = trail.recall_decisions(repo="acme-sample")
            self.assertIn("No prior institutional memory matched", trail.render_recall_markdown(res))
            self.assertIn("No prior institutional memory matched", trail.render_recall_text(res))


class TestStartWorkstreamRecall(unittest.TestCase):
    def test_no_matches_leaves_definition_body_unchanged(self):
        from factonio.orchestrator import start_workstream

        with isolated_hermes_home() as home:
            _register(home)
            res = start_workstream("acme/sample", "Add caching", kind="feature", issue_number=1)
            self.assertEqual(res["recall_count"], 0)
            self.assertNotIn("Prior Institutional Memory", _task(res["task_id"]).body)

    def test_definition_cards_get_bounded_recall_section(self):
        from factonio.orchestrator import RECALL_SECTION_MAX_CHARS, start_workstream

        with isolated_hermes_home() as home:
            from factonio.config import get_repo

            _register(home)
            self.assertEqual(get_repo("acme/sample")["slug"], "acme-sample")
            rows = _seed("acme-sample")
            trail_holdout = rows["reject"]
            for kind in ("feature", "bug", "refactor", "perf", "investigate"):
                with self.subTest(kind):
                    res = start_workstream("acme/sample", "Add write-through caching", kind=kind,
                                           title=f"caching {kind}")
                    self.assertGreater(res["recall_count"], 0, res)
                    body = _task(res["task_id"]).body
                    self.assertIn("## Prior Institutional Memory (Recall)", body)
                    section = body.split("## Prior Institutional Memory (Recall)", 1)[1]
                    self.assertLessEqual(len("## Prior Institutional Memory (Recall)" + section),
                                         RECALL_SECTION_MAX_CHARS + 1)
                    self.assertNotIn("holdout", section)
                    self.assertIn(rows["escape"]["id"], section)
                    self.assertIn(rows["dead_end"]["id"], section)
                    self.assertIn(trail_holdout["id"], section)
                    self.assertIn(rows["keyword"]["id"], section)
                    self.assertNotIn(rows["other_repo"]["id"], section)

    def test_direct_implementation_skips_recall(self):
        from factonio.orchestrator import start_workstream

        with isolated_hermes_home() as home:
            _register(home)
            _seed()
            for kwargs in ({"kind": "quick"}, {"kind": "bug", "skip_define": True}):
                res = start_workstream("acme/sample", "Add write-through caching", **kwargs)
                self.assertEqual(res["phase"], "implement")
                self.assertEqual(res["recall_count"], 0)
                self.assertNotIn("Prior Institutional Memory", _task(res["task_id"]).body)

    def test_own_tenant_is_excluded(self):
        from factonio import trail
        from factonio.orchestrator import make_tenant, start_workstream

        with isolated_hermes_home() as home:
            _register(home)
            first = start_workstream("acme/sample", "Add write-through caching", issue_number=5)
            self.assertEqual(first["tenant"], make_tenant("acme-sample", issue_number=5))
            trail.log_decision("escaped_regression", tenant=first["tenant"], repo_slug="acme-sample",
                               phase="escape", why="same tenant memory", result="recorded")
            again = start_workstream("acme/sample", "Add write-through caching", issue_number=5)
            self.assertEqual(again["recall_count"], 0)
            other = start_workstream("acme/sample", "Add write-through caching", issue_number=6)
            # The escape plus both earlier start_workstream rows of tenant -i5 (keyword "caching").
            self.assertEqual(other["recall_count"], 3)
            self.assertIn("same tenant memory", _task(other["task_id"]).body)

    def test_unreadable_trail_never_blocks_start(self):
        from factonio import trail
        from factonio.orchestrator import start_workstream

        with isolated_hermes_home() as home:
            _register(home)
            trail.trail_db_path().mkdir(parents=True)
            res = start_workstream("acme/sample", "Add caching", kind="feature")
            self.assertTrue(res["ok"])
            self.assertEqual(res["recall_count"], 0)
            self.assertNotIn("Prior Institutional Memory", _task(res["task_id"]).body)


class TestTrailRecallCli(unittest.TestCase):
    def test_recall_formats_limit_and_exit_codes(self):
        from factonio import trail

        with isolated_hermes_home():
            run = _cli()
            code, out = run("trail", "--recall", "--repo", "acme-sample", "--json")
            self.assertEqual(code, 0, out)
            self.assertEqual(json.loads(out)["items"], [])
            self.assertFalse(os.path.lexists(trail.trail_db_path()))

            rows = _seed()
            code, out = run("trail", "--recall", "--repo", "acme-sample", "--query", "write-through caching", "--json")
            self.assertEqual(code, 0, out)
            data = json.loads(out)
            self.assertEqual(data["count"], 4)
            self.assertEqual(data["filters"]["limit"], trail.RECALL_DEFAULT_LIMIT)
            self.assertEqual(data["filters"]["query"], "write-through caching")

            code, out = run("--json", "trail", "--recall", "--repo", "acme-sample", "--limit", "2")
            self.assertEqual(code, 0, out)
            self.assertEqual(json.loads(out)["count"], 2)

            code, out = run("trail", "--recall", "--repo", "acme-sample", "--markdown")
            self.assertEqual(code, 0, out)
            self.assertTrue(out.startswith("## Prior Institutional Memory (Recall)"))
            self.assertIn(rows["escape"]["id"], out)

            code, out = run("trail", "--recall", "--repo", "acme-sample", "--tenant", "acme-sample-old")
            self.assertEqual(code, 0, out)
            self.assertTrue(out.startswith("Recall: 0 item(s)"))

            code, out = run("trail", "--recall", "--audit", "--json")
            self.assertEqual(code, 1, out)

            # Plain listing keeps its 50-row default.
            code, out = run("trail", "--json")
            self.assertEqual(json.loads(out)["filters"]["limit"], 50)

        with isolated_hermes_home():
            trail.trail_db_path().mkdir(parents=True)
            code, out = run("trail", "--recall", "--json")
            self.assertEqual(code, 1, out)
            self.assertFalse(json.loads(out)["ok"])
            code, out = run("trail", "--recall")
            self.assertEqual(code, 1, out)
            self.assertIn("Recall: ERROR", out)


def _blob(res: dict) -> str:
    """Everything recall emits except the caller's own echoed filters."""
    from factonio import trail

    body = {k: v for k, v in res.items() if k != "filters"}
    return json.dumps(body) + trail.render_recall_markdown(res) + trail.render_recall_text(res)


class TestRecallSupersessionChains(unittest.TestCase):
    def test_multi_hop_chain_surfaces_terminal_correction_only(self):
        from factonio import trail

        with isolated_hermes_home():
            a = trail.log_decision("pick_queue", tenant="old", repo_slug="acme-sample", phase="define",
                                   why="alpha: use redis lists", result="adopted")
            b = trail.log_decision("pick_queue", tenant="old", repo_slug="acme-sample", phase="define",
                                   why="beta: use rabbitmq", result="adopted", supersedes_id=a["id"])
            c = trail.log_decision("pick_queue", tenant="old", repo_slug="acme-sample", phase="define",
                                   why="gamma: use sqlite outbox", result="adopted", supersedes_id=b["id"])
            res = trail.recall_decisions(repo="acme-sample")
            self.assertEqual(res["count"], 1, res)
            item = res["items"][0]
            self.assertEqual((item["id"], item["category"]), (a["id"], "superseded"))
            self.assertEqual(item["correction"]["id"], c["id"])
            self.assertEqual([x["id"] for x in item["corrections"]], [c["id"]])
            self.assertFalse(item["correction_out_of_scope"])
            blob = _blob(res)
            self.assertIn("gamma", blob)
            self.assertNotIn(b["id"], blob)
            self.assertNotIn("beta", blob)

    def test_branching_chain_lists_every_current_leaf(self):
        from factonio import trail

        with isolated_hermes_home():
            a = trail.log_decision("pick_db", tenant="old", repo_slug="acme-sample", why="alpha", result="adopted")
            b = trail.log_decision("pick_db", tenant="old", repo_slug="acme-sample", why="beta",
                                   result="adopted", supersedes_id=a["id"])
            c = trail.log_decision("pick_db", tenant="old", repo_slug="acme-sample", why="gamma",
                                   result="adopted", supersedes_id=a["id"])
            d = trail.log_decision("pick_db", tenant="old", repo_slug="acme-sample", why="delta",
                                   result="adopted", supersedes_id=b["id"])
            res = trail.recall_decisions(repo="acme-sample")
            self.assertEqual([i["id"] for i in res["items"]], [a["id"]])
            ids = [x["id"] for x in res["items"][0]["corrections"]]
            self.assertEqual(sorted(ids), sorted([c["id"], d["id"]]))
            self.assertEqual(res["items"][0]["correction"]["id"], ids[0])
            self.assertNotIn(b["id"], _blob(res))
            md = trail.render_recall_markdown(res)
            self.assertIn(c["id"], md)
            self.assertIn(d["id"], md)

    def test_cyclic_lineage_terminates(self):
        from factonio import trail

        with isolated_hermes_home():
            trail.log_decision("seed", tenant="old", repo_slug="acme-sample", why="schema", result="recorded")
            cols = "id, ts, tenant, repo_slug, phase, card_id, actor, decision, why, evidence_json, result, supersedes_id"
            with closing(sqlite3.connect(trail.trail_db_path())) as conn:
                for rid, ts, sup in (("d_cyc_x", "2026-01-01T00:00:00Z", "d_cyc_y"),
                                     ("d_cyc_y", "2026-01-02T00:00:00Z", "d_cyc_x"),
                                     ("d_cyc_z", "2026-01-03T00:00:00Z", "d_cyc_y")):
                    conn.execute(f"INSERT INTO decisions({cols}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (rid, ts, "old", "acme-sample", "define", "", "orchestrator", "loop",
                                  f"why {rid}", "{}", "adopted", sup))
                conn.commit()
            res = trail.recall_decisions(repo="acme-sample")
            self.assertTrue(res["ok"])
            cyc = [i for i in res["items"] if i["id"].startswith("d_cyc")]
            self.assertEqual([i["id"] for i in cyc], ["d_cyc_x"])
            self.assertEqual(sorted(x["id"] for x in cyc[0]["corrections"]), ["d_cyc_z"])


class TestRecallCorrectionScope(unittest.TestCase):
    def test_excluded_tenant_correction_is_never_emitted(self):
        from factonio import trail

        with isolated_hermes_home():
            a = trail.log_decision("pick_auth", tenant="old", repo_slug="probe/repo", phase="define",
                                   why="use sessions", result="adopted")
            b = trail.log_decision("pick_auth", tenant="excluded", repo_slug="probe/repo", phase="define",
                                   why="SECRET-EXCLUDED rationale", result="EXCLUDED-RESULT", supersedes_id=a["id"])
            res = trail.recall_decisions(repo="probe/repo", exclude_tenant="excluded")
            self.assertEqual([i["id"] for i in res["items"]], [a["id"]])
            item = res["items"][0]
            self.assertEqual(item["category"], "superseded")
            self.assertIsNone(item["correction"])
            self.assertEqual(item["corrections"], [])
            self.assertTrue(item["correction_out_of_scope"])
            for needle in (b["id"], "excluded", "SECRET-EXCLUDED", "EXCLUDED-RESULT"):
                self.assertNotIn(needle, _blob(res))

            run = _cli()
            for fmt in ("--json", "--markdown"):
                code, out = run("trail", "--recall", "--repo", "probe/repo", "--tenant", "excluded", fmt)
                self.assertEqual(code, 0, out)
                if fmt == "--json":
                    data = json.loads(out)
                    self.assertEqual(data["filters"]["exclude_tenant"], "excluded")
                    out = json.dumps(data["items"])
                self.assertIn(a["id"], out)
                for needle in (b["id"], "SECRET-EXCLUDED", "EXCLUDED-RESULT"):
                    self.assertNotIn(needle, out)

    def test_chain_through_excluded_tenant_reaches_in_scope_terminal(self):
        from factonio import trail

        with isolated_hermes_home():
            a = trail.log_decision("pick_auth", tenant="old", repo_slug="probe/repo", why="alpha", result="adopted")
            b = trail.log_decision("pick_auth", tenant="excluded", repo_slug="probe/repo", why="beta-excluded",
                                   result="adopted", supersedes_id=a["id"])
            c = trail.log_decision("pick_auth", tenant="older", repo_slug="probe/repo", why="gamma",
                                   result="adopted", supersedes_id=b["id"])
            res = trail.recall_decisions(repo="probe/repo", exclude_tenant="excluded")
            self.assertEqual([i["id"] for i in res["items"]], [a["id"]])
            self.assertEqual(res["items"][0]["correction"]["id"], c["id"])
            self.assertNotIn(b["id"], _blob(res))
            self.assertNotIn("beta-excluded", _blob(res))

    def test_other_repo_correction_is_never_emitted(self):
        from factonio import trail

        with isolated_hermes_home():
            a = trail.log_decision("pick_cache", tenant="one-i1", repo_slug="scope/one", phase="define",
                                   why="use memcached", result="adopted")
            b = trail.log_decision("pick_cache", tenant="two-i1", repo_slug="scope/two", phase="define",
                                   why="SCOPE-TWO rationale", result="SCOPE-TWO-RESULT", supersedes_id=a["id"])
            res = trail.recall_decisions(repo="scope/one")
            self.assertEqual([i["id"] for i in res["items"]], [a["id"]])
            self.assertTrue(res["items"][0]["correction_out_of_scope"])
            for needle in (b["id"], "two-i1", "scope/two", "SCOPE-TWO"):
                self.assertNotIn(needle, _blob(res))
            run = _cli()
            for fmt in ("--json", "--markdown"):
                code, out = run("trail", "--recall", "--repo", "scope/one", fmt)
                self.assertEqual(code, 0, out)
                self.assertIn(a["id"], out)
                for needle in (b["id"], "two-i1", "SCOPE-TWO"):
                    self.assertNotIn(needle, out)
            # Without a repo filter the correction is in scope again.
            both = trail.recall_decisions()
            self.assertEqual(both["items"][0]["correction"]["id"], b["id"])


class TestRecallHoldoutBarrier(unittest.TestCase):
    def test_holdout_in_metadata_never_reaches_definition_card(self):
        from factonio import trail
        from factonio.orchestrator import start_workstream

        with isolated_hermes_home() as home:
            _register(home)
            trail.log_decision(
                "verify_blackbox", tenant="repo-old\n/runs/old/holdout/secret-case.md",
                phase="verify\nholdout/phase-case.md", card_id="t_1 HOLDOUT/card-case.md", repo_slug="acme-sample",
                why="caching broke", result="reject", evidence={"note": "see hold\u200bout/zw-case.md"},
            )
            a = trail.log_decision("pick_cache", tenant="old", repo_slug="acme-sample", phase="define",
                                   why="caching via memcached", result="adopted")
            trail.log_decision(
                "pick_cache", tenant="old\r\n\uff48\uff4f\uff4c\uff44\uff4f\uff55\uff54/fw-case.md",
                phase="define", card_id="hold-out/dash-case.md", repo_slug="acme-sample",
                why="caching via sqlite, not holdout/why-case.md", result="adopted", supersedes_id=a["id"],
            )
            res = trail.recall_decisions(repo="acme-sample", query="caching")
            self.assertEqual(res["count"], 2, res)
            blob = _blob(res)
            for needle in ("holdout", "HOLDOUT", "hold-out", "-case.md", "\n/runs"):
                self.assertNotIn(needle, blob)
            self.assertFalse(trail.contains_holdout_reference(blob))
            for item in res["items"]:
                for key in ("tenant", "phase", "card_id"):
                    self.assertNotIn("\n", item[key])

            started = start_workstream("acme/sample", "Add caching", kind="feature", issue_number=9)
            self.assertEqual(started["recall_count"], 2)
            body = _task(started["task_id"]).body
            self.assertIn("## Prior Institutional Memory (Recall)", body)
            section = body.split("## Prior Institutional Memory (Recall)", 1)[1]
            self.assertFalse(trail.contains_holdout_reference(section))
            self.assertNotIn("-case.md", section)
            self.assertIn(a["id"], section)

    def test_definition_card_drops_recall_section_if_renderer_leaks(self):
        from unittest import mock

        from factonio import trail
        from factonio.orchestrator import start_workstream

        with isolated_hermes_home() as home:
            _register(home)
            _seed()
            leaky = "## Prior Institutional Memory (Recall)\n\n- see /runs/x/holdout/leak.md\n"
            with mock.patch("factonio.orchestrator.render_recall_markdown", return_value=leaky):
                res = start_workstream("acme/sample", "Add write-through caching", kind="feature")
            self.assertTrue(res["ok"])
            self.assertEqual(res["recall_count"], 0)
            body = _task(res["task_id"]).body
            self.assertNotIn("Prior Institutional Memory", body)
            self.assertNotIn("leak.md", body)
            self.assertTrue(trail.contains_holdout_reference("x/Ｈold\u200bOut/y"))
            self.assertFalse(trail.contains_holdout_reference("hold the line; check out main"))


class TestRecallControlObfuscation(unittest.TestCase):
    OBFUSCATED = (
        "h\x00o\x07l\x1bdout/control-probe.md",
        "hol\u200bd\ufeffo\u00adut/control-probe.md",
        "\x1b[31mho\x00ld\u200b\x07out\x1b[0m/control-probe.md",
    )
    LEAKS = ("control-probe", "holdout", "h o l", "hol dout", "h o ldout")

    def _assert_clean(self, text: str) -> None:
        from factonio import trail

        low = text.lower()
        for needle in self.LEAKS:
            self.assertNotIn(needle, low)
        self.assertFalse(trail.contains_holdout_reference(text))

    def test_control_and_format_characters_are_deleted_before_redaction(self):
        from factonio import trail
        from factonio.orchestrator import start_workstream

        for path in self.OBFUSCATED:
            self.assertTrue(trail.contains_holdout_reference(f"see {path}"), repr(path))
            with self.subTest(path=repr(path)), isolated_hermes_home() as home:
                _register(home)
                trail.log_decision(
                    "escaped_regression", tenant=f"acme-old {path}", repo_slug="acme-sample", phase="escape",
                    card_id=f"t_esc {path}", why=f"caching escaped via {path}", result="recorded",
                    evidence={"note": path, path: "key"},
                )
                a = trail.log_decision("pick_cache", tenant="acme-old", repo_slug="acme-sample", phase="define",
                                       why="caching via memcached", result="adopted")
                trail.log_decision(
                    "pick_cache", tenant=f"acme-old\t{path}", repo_slug="acme-sample", phase="define",
                    card_id=path, why=f"caching via sqlite, see {path}", result=f"adopted {path}",
                    supersedes_id=a["id"], evidence={"nested": {"why": path}},
                )
                res = trail.recall_decisions(repo="acme-sample", query="caching")
                self.assertEqual(res["count"], 2, res)
                self._assert_clean(_blob(res))
                run = _cli()
                for fmt in ("--json", "--markdown"):
                    code, out = run("trail", "--recall", "--repo", "acme/sample", fmt)
                    self.assertEqual(code, 0, out)
                    self.assertIn(a["id"], out)
                    self._assert_clean(out)
                started = start_workstream("acme/sample", "Add caching", kind="feature", issue_number=5)
                self.assertEqual(started["recall_count"], 2)
                body = _task(started["task_id"]).body
                section = body.split("## Prior Institutional Memory (Recall)", 1)[1]
                self.assertIn(a["id"], section)
                self._assert_clean(section)
                for needle in ("control-probe", "h o l", "\x00", "\x1b", "\u200b"):
                    self.assertNotIn(needle, body)

    def test_whitespace_controls_still_separate_words(self):
        from factonio import trail

        self.assertEqual(trail._recall_fold("a\tb\nc\r\nd\x00e\u200bf"), "a b c  def")
        self.assertFalse(trail.contains_holdout_reference("hold the line; check out main"))


class TestRecallSpacedHoldoutPaths(unittest.TestCase):
    """Paths with spaces must not leave scenario-name suffixes behind after redaction."""

    PATHS = (
        '"/runs/old/holdout/01 secret-scenario-name.md"',
        '"/synthetic/holdout/private cases/forbidden-scenario.md"',
        "/runs/old/holdout/01 secret-scenario-name.md",
        "/synthetic/holdout/private cases/forbidden-scenario.md",
        "hol\tdout/tab-probe.md",
        "h o l d o u t/tab-probe.md",
    )
    FRAGMENTS = ("secret-scenario-name", "forbidden-scenario", "private cases", "tab-probe", "holdout")

    def _assert_clean(self, text: str) -> None:
        from factonio import trail

        for needle in self.FRAGMENTS:
            self.assertNotIn(needle, text)
        self.assertFalse(trail.contains_holdout_reference(text))

    def test_detection_covers_spaced_markers_but_not_prose(self):
        from factonio import trail

        for path in self.PATHS:
            self.assertTrue(trail.contains_holdout_reference(f"see {path}"), repr(path))
        for prose in ("hold out for review", "Hold out until CI is green", "hold the line; check out main"):
            self.assertFalse(trail.contains_holdout_reference(prose), prose)
            self.assertEqual(trail._recall_redact_text(prose), prose)
        self.assertEqual(trail._recall_redact_text('Review "/a/holdout/b c.md" before retry'), "[redacted]")

    def test_spaced_paths_never_reach_recall_or_definition_card(self):
        from factonio import trail
        from factonio.orchestrator import start_workstream

        for path in self.PATHS:
            with self.subTest(path=repr(path)), isolated_hermes_home() as home:
                _register(home)
                esc = trail.log_decision(
                    "escaped_regression", tenant="acme-old", repo_slug="acme-sample", phase="escape",
                    why=f"Review {path} before retry", result=f"Failed scenario at {path}",
                    evidence={"note": f"caching failed at {path} today", "ok": "hold out for review"},
                )
                a = trail.log_decision("pick_cache", tenant="acme-old", repo_slug="acme-sample", phase="define",
                                       why="caching via memcached", result="adopted")
                trail.log_decision("pick_cache", tenant="acme-old", repo_slug="acme-sample", phase="define",
                                   why=f"caching via sqlite; see {path} for why", result="adopted",
                                   supersedes_id=a["id"])
                res = trail.recall_decisions(repo="acme-sample", query="caching")
                self.assertEqual(res["count"], 2, res)
                self._assert_clean(_blob(res))
                item = next(i for i in res["items"] if i["id"] == esc["id"])
                self.assertEqual((item["why"], item["result"]), ("[redacted]", "[redacted]"))
                self.assertEqual(item["evidence"], {"note": "[redacted]", "ok": "hold out for review"})
                run = _cli()
                for fmt in ("--json", "--markdown"):
                    code, out = run("trail", "--recall", "--repo", "acme/sample", fmt)
                    self.assertEqual(code, 0, out)
                    self.assertIn(a["id"], out)
                    self.assertIn(esc["id"], out)
                    self._assert_clean(out)
                started = start_workstream("acme/sample", "Add caching", kind="feature", issue_number=5)
                self.assertEqual(started["recall_count"], 2)
                body = _task(started["task_id"]).body
                section = body.split("## Prior Institutional Memory (Recall)", 1)[1]
                self.assertIn(a["id"], section)
                self.assertIn(esc["id"], section)
                self._assert_clean(section)

    def test_renderer_withholds_whole_line_for_unscrubbed_items(self):
        from factonio import trail

        recall = {"ok": True, "items": [{"category": "escapes", "id": "d1", "decision": "x",
                                         "why": 'Review "/runs/holdout/01 secret-scenario-name.md" now'}],
                  "counts": {"escapes": 1}}
        for out in (trail.render_recall_markdown(recall), trail.render_recall_text(recall)):
            self.assertIn("[redacted]", out)
            self._assert_clean(out)


class TestRecallAnsiEscapeSequences(unittest.TestCase):
    """Complete terminal escape sequences inside the marker must not leave printable parameters behind."""

    PATHS = (
        "/runs/old/ho\x1b[31mldout\x1b[0m/ansi-secret-case.md",
        "/runs/old/ho\x1b]8;;https://x\x07ldout/ansi-secret-case.md",
        "/runs/old/ho\x1b]0;title\x1b\\ldout/ansi-secret-case.md",
        "/runs/old/ho\x9b31mldout\x9b0m/ansi-secret-case.md",
        "/runs/old/ho\x9d8;;x\x9cldout/ansi-secret-case.md",
        "/runs/old/ho\x1bPq#0\x1b\\ldout/ansi-secret-case.md",
        "/runs/old/ho\x1b(Bldout/ansi-secret-case.md",
        "/runs/old/ho\x1b\x00[1;31mld\x1b[0mout/ansi-secret-case.md",
        "/runs/old/ho\x1b\uff3b31mldout/ansi-secret-case.md",
    )
    FRAGMENTS = ("ansi-secret-case", "holdout", "[31m", "[0m", "31mldout", "\x1b", "\x9b", "\x9d")

    def _assert_clean(self, text: str) -> None:
        from factonio import trail

        for needle in self.FRAGMENTS:
            self.assertNotIn(needle, text)
        self.assertFalse(trail.contains_holdout_reference(text))

    def test_detection_and_whole_field_redaction(self):
        from factonio import trail

        for path in self.PATHS:
            self.assertTrue(trail.contains_holdout_reference(path), repr(path))
            self.assertEqual(trail._recall_redact_text(f"see {path} now"), "[redacted]", repr(path))
        colored = "\x1b[32mPASS\x1b[0m: 505 tests"
        self.assertFalse(trail.contains_holdout_reference(colored))
        self.assertEqual(trail._recall_redact_text(colored), "PASS: 505 tests")
        self.assertFalse(trail.contains_holdout_reference("\x1b[1mhold out\x1b[0m for review"))

    def test_ansi_paths_never_reach_recall_or_definition_card(self):
        from factonio import trail
        from factonio.orchestrator import start_workstream

        for path in self.PATHS:
            with self.subTest(path=repr(path)), isolated_hermes_home() as home:
                _register(home)
                esc = trail.log_decision(
                    "escaped_regression", tenant="acme-old", repo_slug="acme-sample", phase="escape",
                    card_id=f"t_esc {path}", why=f"caching escaped via {path}", result=f"Failed at {path}",
                    evidence={"note": path, path: "key", "nested": {"why": path}},
                )
                a = trail.log_decision("pick_cache", tenant="acme-old", repo_slug="acme-sample", phase="define",
                                       why="caching via memcached", result="adopted")
                trail.log_decision("pick_cache", tenant="acme-old", repo_slug="acme-sample", phase="define",
                                   why=f"caching via sqlite; see {path}", result="adopted",
                                   supersedes_id=a["id"])
                res = trail.recall_decisions(repo="acme-sample", query="caching")
                self.assertEqual(res["count"], 2, res)
                self._assert_clean(_blob(res))
                item = next(i for i in res["items"] if i["id"] == esc["id"])
                self.assertEqual((item["why"], item["result"]), ("[redacted]", "[redacted]"))
                run = _cli()
                for fmt in ("--json", "--markdown"):
                    code, out = run("trail", "--recall", "--repo", "acme/sample", fmt)
                    self.assertEqual(code, 0, out)
                    self.assertIn(a["id"], out)
                    self.assertIn(esc["id"], out)
                    self._assert_clean(out)
                started = start_workstream("acme/sample", "Add caching", kind="feature", issue_number=5)
                self.assertEqual(started["recall_count"], 2)
                body = _task(started["task_id"]).body
                section = body.split("## Prior Institutional Memory (Recall)", 1)[1]
                self.assertIn(a["id"], section)
                self.assertIn(esc["id"], section)
                self._assert_clean(section)
                for needle in ("ansi-secret-case", "\x1b", "\x9b", "\x9d", "[31m"):
                    self.assertNotIn(needle, body)


class TestRecallRepoAliases(unittest.TestCase):
    def _history(self, repo: str, tenant: str) -> dict[str, dict[str, Any]]:
        from factonio import trail

        rows = {
            "escape": trail.log_decision("escaped_regression", tenant=tenant, repo_slug=repo, phase="escape",
                                         why="arena scoring escaped verification", result="recorded"),
            "dead_end": trail.log_decision("pick_arena_store", tenant=tenant, repo_slug=repo, phase="define",
                                           why="arena store in redis", result="adopted"),
            "reject": trail.log_decision("verify_blackbox", tenant=tenant, repo_slug=repo, phase="verify",
                                         why="arena leaderboard broke", result="reject"),
        }
        rows["correction"] = trail.log_decision(
            "pick_arena_store", tenant=tenant, repo_slug=repo, phase="define", why="arena store in sqlite",
            result="adopted", supersedes_id=rows["dead_end"]["id"],
        )
        return rows

    def _stored_repo_slugs(self, home: Path) -> set[str]:
        from factonio import trail

        with closing(sqlite3.connect(trail.trail_db_path())) as conn:
            return {r[0] for r in conn.execute("SELECT repo_slug FROM decisions")}

    def test_start_recalls_history_logged_under_owner_repo_or_slug(self):
        from factonio import trail
        from factonio.orchestrator import start_workstream

        for label in ("owner_repo", "slug", "both"):
            with self.subTest(label), isolated_hermes_home() as home:
                ids: set[str] = set()
                if label in ("owner_repo", "both"):
                    # Logged before registration, so the documented `owner/repo` identifier is stored verbatim.
                    rows = self._history("acme/arena-demo", "acme-arena-demo-old")
                    ids |= {rows["escape"]["id"], rows["dead_end"]["id"], rows["reject"]["id"]}
                _register(home, "acme/arena-demo")
                if label in ("slug", "both"):
                    rows = self._history("acme-arena-demo", "acme-arena-demo-older")
                    ids |= {rows["escape"]["id"], rows["dead_end"]["id"], rows["reject"]["id"]}
                if label == "both":
                    self.assertEqual(self._stored_repo_slugs(home), {"acme/arena-demo", "acme-arena-demo"})
                for spec in ("acme/arena-demo", "acme-arena-demo", "ACME/Arena-Demo"):
                    res = trail.recall_decisions(repo=spec, query="arena")
                    self.assertEqual({i["id"] for i in res["items"]}, ids, spec)
                started = start_workstream("acme/arena-demo", "Improve arena scoring", kind="feature")
                self.assertTrue(started["ok"], started)
                self.assertEqual(started["recall_count"], len(ids))
                body = _task(started["task_id"]).body
                section = body.split("## Prior Institutional Memory (Recall)", 1)[1]
                for row_id in ids:
                    self.assertEqual(section.count(f"`{row_id}`"), 1, row_id)
                self.assertIn("corrected by", section)

    def test_distinct_repositories_stay_isolated(self):
        from factonio import trail

        with isolated_hermes_home() as home:
            one = self._history("scope/one", "one-i1")
            two = self._history("scope/two", "two-i1")
            trail.log_decision("pick_arena_store", tenant="two-i2", repo_slug="scope-two", phase="define",
                               why="SCOPE-TWO correction", result="adopted", supersedes_id=one["dead_end"]["id"])
            _register(home, "scope/one")
            for spec in ("scope/one", "scope-one", ["scope/one", "scope-one"]):
                res = trail.recall_decisions(repo=spec, query="arena")
                blob = _blob(res)
                self.assertIn(one["escape"]["id"], blob)
                self.assertIn(one["correction"]["id"], blob)
                for row in two.values():
                    self.assertNotIn(row["id"], blob)
                for needle in ("two-i1", "two-i2", "SCOPE-TWO", "scope/two", "scope-two"):
                    self.assertNotIn(needle, blob)


class TestSkillPrinciplesAndCrashPickup(unittest.TestCase):
    @staticmethod
    def _read(rel: str) -> str:
        return (PLUGIN_ROOT / rel).read_text(encoding="utf-8")

    def test_define_core_principles_and_recall(self):
        text = self._read("skills/define/SKILL.md")
        principles = text.split("## Core Principles", 1)[1].split("\n## ", 1)[0]
        for needle in ("**Build the Lever:**", "**Idempotency:**", "**Minimize Reader Load:**",
                       '**"No is an acceptable answer":**'):
            self.assertIn(needle, principles)
        ground = text.split("### 1. Ground in the Live Repository", 1)[1].split("### 1b.", 1)[0]
        self.assertIn("hermes factonio trail --recall --repo <repo>", ground)
        self.assertIn("## Prior Institutional Memory (Recall)", ground)

    def test_implement_crash_pickup_protocol(self):
        text = self._read("skills/implement/SKILL.md")
        self.assertLess(text.index("### 1. Orient in the Worktree"), text.index("### 1b. Crash-Pickup Protocol"))
        self.assertLess(text.index("### 1b. Crash-Pickup Protocol"), text.index("### 2. Implement and Verify Locally"))
        crash = text.split("### 1b. Crash-Pickup Protocol", 1)[1].split("### 2.", 1)[0]
        for needle in ("**Resume**", "**Cold-restart**", "corrupted or circular", "gh pr list --head",
                       "never open a second PR", "**Idempotency**", "**Minimize Reader Load**", "**Build the Lever**"):
            self.assertIn(needle, crash)
        self.assertIn("Never search for, open, or reference any `holdout/` directory", text)

    def test_orchestrator_pushback_recall_and_crash_pickup_outside_merge_gate(self):
        text = self._read("skills/orchestrator/SKILL.md")
        intake = text.split("### 1. Intake and Clarification", 1)[1].split("### 2.", 1)[0]
        self.assertIn('**"No is an acceptable answer":**', intake)
        trail_section = text.split("## Decision Trail", 1)[1].split("\n## ", 1)[0]
        self.assertIn("**Pre-workstream recall:**", trail_section)
        self.assertIn("hermes factonio trail --recall --repo <repo>", trail_section)
        self.assertIn("`recall_count`", trail_section)
        errors = text.split("## Error Handling", 1)[1].split("\n## ", 1)[0]
        self.assertIn("**Crash pickup (reclaimed or crashed card):**", errors)
        merge_gate = text.split("### 5. Merge Gate", 1)[1].split("\n## ", 1)[0]
        for needle in ("recall", "Crash pickup", "No is an acceptable answer"):
            self.assertNotIn(needle, merge_gate)

    def test_orchestrator_prompt_section_stays_small(self):
        import factonio

        self.assertLess(len(factonio.ORCHESTRATOR_PROMPT_SECTION), 2900)


if __name__ == "__main__":
    unittest.main()
