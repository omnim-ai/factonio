"""Clean-room tests for watchdog tick detection, metrics aggregation, and dashboard API."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import time
import unittest
from pathlib import Path

from tests._env import PLUGIN_ROOT, isolated_hermes_home

if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))


class _FakeState:
    def __init__(self) -> None:
        self._data: dict = {}

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def set(self, key: str, value) -> None:
        self._data[key] = value


class _FakeCtx:
    def __init__(self) -> None:
        self.state = _FakeState()
        self.events: list[tuple[str, dict]] = []

    def emit(self, name: str, payload: dict) -> None:
        self.events.append((name, payload))


def _seed_kanban_db(db_path: Path, now_ts: float) -> None:
    from hermes_cli.kanban_db_connect import connect_closing

    with connect_closing(db_path=db_path) as conn:
        # 1. Done Definition card (120s)
        conn.execute(
            """
            INSERT INTO tasks (id, title, assignee, status, tenant, session_id, skills, created_at, started_at, completed_at)
            VALUES (?, ?, 'factonio', 'done', 'acme-api-i1', 'sess_1', ?, ?, ?, ?)
            """,
            ("t_def1", "[define] Add webhooks", json.dumps(["factonio:define"]), now_ts - 600, now_ts - 600, now_ts - 480),
        )
        conn.execute(
            """
            INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome, metadata)
            VALUES ('t_def1', 'factonio', 'completed', ?, ?, 'completed', '{}')
            """,
            (now_ts - 600, now_ts - 480),
        )

        # 2. Done Implement card with 2 runs (rework!)
        conn.execute(
            """
            INSERT INTO tasks (id, title, assignee, status, tenant, session_id, skills, created_at, started_at, completed_at)
            VALUES (?, ?, 'factonio', 'done', 'acme-api-i1', 'sess_1', ?, ?, ?, ?)
            """,
            ("t_imp1", "[implement] Webhook delivery", json.dumps(["factonio:implement"]), now_ts - 470, now_ts - 470, now_ts - 200),
        )
        conn.execute(
            """
            INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome, metadata)
            VALUES ('t_imp1', 'factonio', 'completed', ?, ?, 'completed', ?)
            """,
            (now_ts - 470, now_ts - 350, json.dumps({"repo": "acme/api", "pr_number": 9})),
        )
        conn.execute(
            """
            INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome, metadata)
            VALUES ('t_imp1', 'factonio', 'completed', ?, ?, 'completed', ?)
            """,
            (now_ts - 300, now_ts - 200, json.dumps({"repo": "acme/api", "pr_number": 9, "published_pr": "https://github.com/acme/api/pull/9"})),
        )

        # 3. Done Diff Check (pass) + Black-Box Check (pass)
        conn.execute(
            """
            INSERT INTO tasks (id, title, assignee, status, tenant, session_id, skills, created_at, started_at, completed_at)
            VALUES ('t_vdiff1', '[verify:diff] acme/api#9', 'factonio', 'done', 'acme-api-i1', 'sess_1', ?, ?, ?, ?)
            """,
            (json.dumps(["factonio:verify-diff"]), now_ts - 190, now_ts - 190, now_ts - 130),
        )
        conn.execute(
            """
            INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome, metadata)
            VALUES ('t_vdiff1', 'factonio', 'completed', ?, ?, 'completed', ?)
            """,
            (now_ts - 190, now_ts - 130, json.dumps({"verdict": "pass"})),
        )
        conn.execute(
            """
            INSERT INTO tasks (id, title, assignee, status, tenant, session_id, skills, created_at, started_at, completed_at)
            VALUES ('t_vbb1', '[verify:blackbox] acme/api#9', 'factonio', 'done', 'acme-api-i1', 'sess_1', ?, ?, ?, ?)
            """,
            (json.dumps(["factonio:verify-blackbox"]), now_ts - 190, now_ts - 190, now_ts - 110),
        )
        conn.execute(
            """
            INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome, metadata)
            VALUES ('t_vbb1', 'factonio', 'completed', ?, ?, 'completed', ?)
            """,
            (now_ts - 190, now_ts - 110, json.dumps({"verdict": "pass"})),
        )

        # 4. Stale running card (silent 35m) + triage card + escape card
        conn.execute(
            """
            INSERT INTO tasks (id, title, assignee, status, tenant, session_id, skills, created_at, started_at, last_heartbeat_at)
            VALUES ('t_stale1', '[implement] Stuck task', 'factonio', 'running', 'acme-api-i2', 'sess_2', ?, ?, ?, ?)
            """,
            (json.dumps(["factonio:implement"]), now_ts - 2500, now_ts - 2500, now_ts - 2100),
        )
        conn.execute(
            """
            INSERT INTO task_runs (task_id, profile, status, started_at, last_heartbeat_at)
            VALUES ('t_stale1', 'factonio', 'running', ?, ?)
            """,
            (now_ts - 2500, now_ts - 2100),
        )
        conn.execute(
            """
            INSERT INTO tasks (id, title, assignee, status, tenant, session_id, skills, result, created_at)
            VALUES ('t_esc1', '[escape] [define] Missed timeout bug', 'factonio', 'triage', 'acme-api-i3', 'sess_3', ?, 'Escalated', ?)
            """,
            (json.dumps(["factonio:define"]), now_ts - 100),
        )
        conn.commit()


class TestWatchdogMetricsAndApi(unittest.TestCase):
    def test_watchdog_detects_throttles_and_deduplicates(self):
        from factonio.watchdog import run_watchdog_tick

        with isolated_hermes_home() as home:
            now_ts = time.time()
            db_path = home / "kanban.db"
            _seed_kanban_db(db_path, now_ts)
            ctx = _FakeCtx()

            # First tick emits 2 alerts (stale_running + triage)
            first = run_watchdog_tick(ctx, now=now_ts, db_path_override=db_path)
            self.assertTrue(first["ran"])
            kinds = {a["kind"] for a in first["alerts"]}
            self.assertEqual(kinds, {"stale_running", "triage"})
            self.assertEqual(len(ctx.events), 2)

            # Second tick 60s later is throttled
            second = run_watchdog_tick(ctx, now=now_ts + 60, db_path_override=db_path)
            self.assertFalse(second["ran"])
            self.assertEqual(second["reason"], "throttled")

            # Third tick 6 minutes later scans again but deduplicates unchanged alerts
            third = run_watchdog_tick(ctx, now=now_ts + 360, db_path_override=db_path)
            self.assertTrue(third["ran"])
            self.assertEqual(third["alerts"], [])

    def test_metrics_and_dashboard_api_routes(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        with isolated_hermes_home() as home:
            now_ts = time.time()
            _seed_kanban_db(home / "kanban.db", now_ts)

            spec = importlib.util.spec_from_file_location(
                "factonio_test_plugin_api", PLUGIN_ROOT / "dashboard" / "plugin_api.py"
            )
            assert spec is not None and spec.loader is not None
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            app = FastAPI()
            app.include_router(mod.router, prefix="/api/plugins/factonio")
            client = TestClient(app)

            r_metrics = client.get("/api/plugins/factonio/metrics?range=30d")
            self.assertEqual(r_metrics.status_code, 200)
            m = r_metrics.json()
            self.assertEqual(m["totals"]["workstreams"], 3)
            self.assertEqual(m["verification"]["diff"]["pass"], 1)
            self.assertEqual(m["verification"]["blackbox"]["pass"], 1)
            self.assertEqual(m["rework"]["cards_with_rework"], 1)
            self.assertEqual(m["escapes"]["count"], 1)
            self.assertEqual(m["phases"]["define"]["median_seconds"], 120.0)

            r_cards = client.get("/api/plugins/factonio/cards?session_id=sess_1")
            self.assertEqual(r_cards.status_code, 200)
            cards = r_cards.json()["cards"]
            self.assertEqual(len(cards), 4)
            imp_card = next(c for c in cards if c["id"] == "t_imp1")
            self.assertEqual(imp_card["pr"]["number"], 9)

            for ep in ("/status", "/repos", "/models", "/doctor"):
                resp = client.get(f"/api/plugins/factonio{ep}")
                self.assertEqual(resp.status_code, 200, f"Endpoint {ep} failed: {resp.text}")

    def test_repos_api_get_defaults_and_patch(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from unittest import mock

        from factonio.config import save_settings
        from factonio.repos import add_repo

        with isolated_hermes_home() as home:
            local = home / "dev" / "sample"
            (local / ".git").mkdir(parents=True)
            add_repo("acme/sample", local_path=str(local), clone=False, inspect_remote=False)
            save_settings({"design_provider": "auto", "design_approval": "ask"})

            spec = importlib.util.spec_from_file_location(
                "factonio_test_plugin_api_repos", PLUGIN_ROOT / "dashboard" / "plugin_api.py"
            )
            assert spec is not None and spec.loader is not None
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            app = FastAPI()
            app.include_router(mod.router, prefix="/api/plugins/factonio")
            client = TestClient(app)
            base = "/api/plugins/factonio"
            no_claude = {"claude_bin": None, "has_credentials": False, "available": False}

            with mock.patch.object(mod._config, "detect_claude_design_support", return_value=no_claude):
                r = client.get(f"{base}/repos")
                self.assertEqual(r.status_code, 200)
                body = r.json()
                self.assertEqual(
                    body["defaults"],
                    {"merge_policy": "auto", "publish_policy": "auto", "max_fix_rounds": 3, "design_provider": "auto",
                     "design_approval": "ask", "default_rigor": "standard"},
                )
                item = body["repos"][0]
                for key in ("slug", "repo", "url", "path", "default_branch", "merge_policy", "publish_policy", "max_fix_rounds",
                            "has_required_checks", "models"):
                    self.assertIn(key, item)
                self.assertIsNone(item["design_provider"])
                self.assertIsNone(item["design_approval"])
                self.assertEqual(item["effective_design_provider"], "auto")
                self.assertEqual(item["effective_design_approval"], "ask")
                self.assertEqual(item["resolved_design_provider"], "hermes")

                r = client.patch(
                    f"{base}/repos/acme-sample",
                    json={"merge_policy": "ask", "max_fix_rounds": 4, "default_branch": "develop",
                          "design_provider": "off", "design_approval": "auto"},
                )
                self.assertEqual(r.status_code, 200, r.text)
                body = r.json()
                self.assertTrue(body["ok"])
                repo = body["repo"]
                self.assertEqual(repo["merge_policy"], "ask")
                self.assertEqual(repo["max_fix_rounds"], 4)
                self.assertEqual(repo["default_branch"], "develop")
                self.assertEqual(repo["design_provider"], "off")
                self.assertEqual(repo["effective_design_provider"], "off")
                self.assertEqual(repo["resolved_design_provider"], "off")
                self.assertEqual(repo["design_approval"], "auto")

                r = client.patch(f"{base}/repos/acme-sample", json={"design_provider": None, "design_approval": "inherit"})
                self.assertEqual(r.status_code, 200, r.text)
                repo = r.json()["repo"]
                self.assertIsNone(repo["design_provider"])
                self.assertIsNone(repo["design_approval"])
                self.assertEqual(repo["effective_design_provider"], "auto")
                self.assertEqual(repo["effective_design_approval"], "ask")

                store = json.loads((home / "plugin-data" / "factonio" / "repos.json").read_text(encoding="utf-8"))
                before = store
                bad = [
                    {},
                    [],
                    {"slug": "x"},
                    {"repo": "evil/repo"},
                    {"path": "/tmp"},
                    {"models": {}},
                    {"merge_policy": "ask", "bogus": 1},
                    {"merge_policy": "always"},
                    {"max_fix_rounds": 0},
                    {"max_fix_rounds": "3"},
                    {"max_fix_rounds": True},
                    {"default_branch": ""},
                    {"default_branch": "a b"},
                    {"design_provider": "nope"},
                ]
                for payload in bad:
                    with self.subTest(payload=payload):
                        r = client.patch(f"{base}/repos/acme-sample", json=payload)
                        self.assertEqual(r.status_code, 422, r.text)
                after = json.loads((home / "plugin-data" / "factonio" / "repos.json").read_text(encoding="utf-8"))
                self.assertEqual(before, after)

                r = client.patch(f"{base}/repos/nope-missing", json={"merge_policy": "ask"})
                self.assertEqual(r.status_code, 404, r.text)

                r = client.get(f"{base}/repos")
                self.assertEqual(r.json()["repos"][0]["merge_policy"], "ask")

    def test_repos_api_publish_policy(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from factonio.config import list_repos, save_repos_store, save_settings
        from factonio.repos import add_repo

        with isolated_hermes_home() as home:
            for name in ("sample", "legacy"):
                local = home / "dev" / name
                (local / ".git").mkdir(parents=True)
                add_repo(f"acme/{name}", local_path=str(local), clone=False, inspect_remote=False)
            store = list_repos()
            store["acme-legacy"].pop("publish_policy", None)
            save_repos_store(store)
            save_settings({"default_publish_policy": "ask"})

            spec = importlib.util.spec_from_file_location(
                "factonio_test_plugin_api_publish", PLUGIN_ROOT / "dashboard" / "plugin_api.py"
            )
            assert spec is not None and spec.loader is not None
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            app = FastAPI()
            app.include_router(mod.router, prefix="/api/plugins/factonio")
            client = TestClient(app)
            base = "/api/plugins/factonio"
            repos_json = home / "plugin-data" / "factonio" / "repos.json"

            body = client.get(f"{base}/repos").json()
            self.assertEqual(body["defaults"]["publish_policy"], "ask")
            by_slug = {r["slug"]: r for r in body["repos"]}
            # add_repo seeded the explicit value at registration time (global was auto then).
            self.assertEqual(by_slug["acme-sample"]["publish_policy"], "auto")
            # Legacy records without the key inherit the current global default.
            self.assertNotIn("publish_policy", json.loads(repos_json.read_text(encoding="utf-8"))["acme-legacy"])
            self.assertEqual(by_slug["acme-legacy"]["publish_policy"], "ask")

            for value in ("ask", "auto"):
                with self.subTest(value=value):
                    r = client.patch(f"{base}/repos/acme-sample", json={"publish_policy": value})
                    self.assertEqual(r.status_code, 200, r.text)
                    self.assertEqual(r.json()["repo"]["publish_policy"], value)
                    stored = json.loads(repos_json.read_text(encoding="utf-8"))["acme-sample"]
                    self.assertEqual(stored["publish_policy"], value)

            before = repos_json.read_text(encoding="utf-8")
            for bad in ("always", None, "", 1, True, ["ask"]):
                with self.subTest(bad=bad):
                    r = client.patch(f"{base}/repos/acme-sample", json={"publish_policy": bad})
                    self.assertEqual(r.status_code, 422, r.text)
            self.assertEqual(repos_json.read_text(encoding="utf-8"), before)

            # Invalid global config never reads as "auto": GET stays 200 and reports null.
            save_settings({"default_publish_policy": "sometimes"})
            r = client.get(f"{base}/repos")
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertIsNone(body["defaults"]["publish_policy"])
            by_slug = {x["slug"]: x for x in body["repos"]}
            self.assertIsNone(by_slug["acme-legacy"]["publish_policy"])
            self.assertEqual(by_slug["acme-sample"]["publish_policy"], "auto")

    def test_dashboard_manifest_discovered_by_hermes(self):
        from hermes_cli.web_server_dashboard import _discover_dashboard_plugins

        with isolated_hermes_home() as home:
            plugins_dir = home / "plugins"
            plugins_dir.mkdir(parents=True, exist_ok=True)
            (plugins_dir / "factonio").symlink_to(PLUGIN_ROOT)

            discovered = {p.get("name"): p for p in _discover_dashboard_plugins()}
            self.assertIn("factonio", discovered)
            entry = discovered["factonio"]
            self.assertTrue(entry.get("has_api"))
            self.assertEqual(entry.get("_api_file"), "plugin_api.py")
            self.assertTrue((PLUGIN_ROOT / "dashboard" / "manifest.json").is_file())
            manifest = json.loads((PLUGIN_ROOT / "dashboard" / "manifest.json").read_text(encoding="utf-8"))
            self.assertNotIn("read-only", manifest["description"].lower())


def _seed_enrichment_db(db_path: Path, now_ts: float) -> None:
    """Cards exercising enriched /cards metadata: recovery, rejects, live runs, statuses."""
    from hermes_cli.kanban_db_connect import connect_closing

    def task(conn, tid, title, status, tenant, session, skill, created, **extra):
        cols = ["id", "title", "assignee", "status", "tenant", "session_id", "skills", "created_at", *extra]
        vals = [tid, title, "factonio", status, tenant, session, json.dumps([skill]), created, *extra.values()]
        conn.execute(
            f"INSERT INTO tasks ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", vals
        )

    def run(conn, tid, status, started, ended=None, outcome=None, metadata=None, summary=None, error=None):
        conn.execute(
            """
            INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome, metadata, summary, error)
            VALUES (?, 'factonio', ?, ?, ?, ?, ?, ?, ?)
            """,
            (tid, status, started, ended, outcome, json.dumps(metadata or {}), summary, error),
        )

    with connect_closing(db_path=db_path) as conn:
        # Implement card: run 1 crashed, run 2 completed cleanly; stale last_failure_error remains.
        task(conn, "t_rec", "[implement] Recovered", "done", "ws-a", "sess_a", "factonio:implement",
             now_ts - 1000, started_at=now_ts - 900, completed_at=now_ts - 500,
             result="Implemented PR", last_failure_error="Traceback: boom")
        run(conn, "t_rec", "crashed", now_ts - 1000, now_ts - 900, "crashed", error="Traceback: boom")
        run(conn, "t_rec", "completed", now_ts - 800, now_ts - 500, "completed",
            {"repo": "acme/api", "pr_number": 7}, summary="Opened PR #7")

        # Diff verify rejected via explicit metadata verdict (upper-case -> normalized).
        task(conn, "t_vd_rej", "[verify:diff] acme/api#7", "done", "ws-a", "sess_a", "factonio:verify-diff",
             now_ts - 450, started_at=now_ts - 450, completed_at=now_ts - 400)
        run(conn, "t_vd_rej", "completed", now_ts - 450, now_ts - 400, "completed",
            {"verdict": "REJECT"}, summary="Missing tests")

        # Black-box verify: no metadata verdict, latest outcome changes_requested -> fallback reject.
        task(conn, "t_vb_cr", "[verify:blackbox] acme/api#7", "ready", "ws-a", "sess_a", "factonio:verify-blackbox",
             now_ts - 440, started_at=now_ts - 440, result="Holdout 2 failed")
        run(conn, "t_vb_cr", "changes_requested", now_ts - 440, now_ts - 380, "changes_requested",
            summary="Holdout 2 failed")

        # Black-box verify done without verdict metadata -> fallback pass.
        task(conn, "t_vb_ok", "[verify:blackbox] acme/api#8", "done", "ws-b", "sess_b", "factonio:verify-blackbox",
             now_ts - 300, started_at=now_ts - 300, completed_at=now_ts - 250, result="All holdouts passed")
        run(conn, "t_vb_ok", "completed", now_ts - 300, now_ts - 250, "completed")

        # Running implement card: one finished crashed run (60s) + live in-flight run (120s).
        task(conn, "t_live", "[implement] Live", "running", "ws-b", "sess_b", "factonio:implement",
             now_ts - 400, started_at=now_ts - 120, last_failure_error="spawn failed")
        run(conn, "t_live", "crashed", now_ts - 400, now_ts - 340, "spawn_failed", error="spawn failed")
        run(conn, "t_live", "running", now_ts - 120)

        # Blocked card: summary from run, but result not used as summary fallback when blocked.
        task(conn, "t_blk", "[implement] Blocked", "blocked", "ws-b", "sess_b", "factonio:implement",
             now_ts - 200, block_kind="needs_input", result="Need API key")
        # Triage card without runs.
        task(conn, "t_tri", "[escape] [define] Escaped", "triage", "ws-c", "sess_c", "factonio:define",
             now_ts - 150, result="Escalated")
        # Todo + review cards without runs.
        task(conn, "t_todo", "[implement] Later", "todo", "ws-c", "sess_c", "factonio:implement", now_ts - 140)
        task(conn, "t_rev", "[implement] In review", "review", "ws-c", "sess_c", "factonio:implement", now_ts - 130)
        conn.commit()


class TestCardsEnrichment(unittest.TestCase):
    NEW_FIELDS = ("started_at", "session_id", "active_duration_seconds", "verdict", "summary",
                  "last_outcome", "last_error")

    def _assert_enriched(self, data: dict, now_ts: float) -> None:
        cards = {c["id"]: c for c in data["cards"]}
        for c in cards.values():
            for field in self.NEW_FIELDS:
                self.assertIn(field, c, f"{c['id']} missing {field}")
            # Legacy keys preserved.
            for legacy in ("id", "title", "status", "phase", "tenant", "parents", "model", "provider",
                           "block_kind", "block_reason", "pr", "runs", "elapsed_seconds", "created_at",
                           "completed_at"):
                self.assertIn(legacy, c)

        rec = cards["t_rec"]
        self.assertEqual(rec["session_id"], "sess_a")
        self.assertAlmostEqual(rec["started_at"], now_ts - 900, delta=1)
        self.assertEqual(rec["runs"], 2)
        self.assertEqual(rec["last_outcome"], "completed")
        self.assertIsNone(rec["last_error"])  # clean retry clears stale failures
        self.assertEqual(rec["summary"], "Opened PR #7")
        self.assertIsNone(rec["verdict"])
        self.assertAlmostEqual(rec["active_duration_seconds"], 100 + 300, delta=2)
        self.assertEqual(rec["pr"]["number"], 7)

        vd = cards["t_vd_rej"]
        self.assertEqual(vd["verdict"], "reject")
        self.assertEqual(vd["summary"], "Missing tests")
        self.assertIsNone(vd["last_error"])

        vb_cr = cards["t_vb_cr"]
        self.assertEqual(vb_cr["verdict"], "reject")
        self.assertEqual(vb_cr["last_outcome"], "changes_requested")
        self.assertIsNone(vb_cr["last_error"])

        vb_ok = cards["t_vb_ok"]
        self.assertEqual(vb_ok["verdict"], "pass")
        self.assertEqual(vb_ok["summary"], "All holdouts passed")  # falls back to tasks.result

        live = cards["t_live"]
        self.assertIsNone(live["last_outcome"])
        self.assertEqual(live["last_error"], "spawn failed")  # task not done -> last_failure_error
        self.assertGreaterEqual(live["active_duration_seconds"], 60 + 120 - 2)
        self.assertLessEqual(live["active_duration_seconds"], 60 + 120 + 30)

        blk = cards["t_blk"]
        self.assertIsNone(blk["summary"])  # result is the block reason, not a summary
        self.assertEqual(blk["block_reason"], "Need API key")
        self.assertIsNone(blk["active_duration_seconds"])
        self.assertIsNone(blk["last_outcome"])
        self.assertIsNone(blk["verdict"])
        self.assertIsNone(cards["t_tri"]["summary"])
        self.assertIsNone(cards["t_todo"]["started_at"])

    def _assert_all_counts(self, counts: dict) -> None:
        self.assertEqual(counts, {
            "running": 1, "blocked": 1, "triage": 1, "ready": 1, "done": 3,
            "todo": 1, "review": 1, "rejected": 2, "inconclusive": 0, "attention": 4, "total": 9,
        })

    def test_thread_cards_enriched_fields_and_counts(self):
        from factonio import metrics

        with isolated_hermes_home() as home:
            now_ts = time.time()
            db_path = home / "kanban.db"
            _seed_enrichment_db(db_path, now_ts)

            all_cards = metrics.thread_cards(db_path)
            self._assert_enriched(all_cards, now_ts)
            self._assert_all_counts(all_cards["counts"])

            sess_a = metrics.thread_cards(db_path, session_id="sess_a")
            self.assertEqual({c["id"] for c in sess_a["cards"]}, {"t_rec", "t_vd_rej", "t_vb_cr"})
            self.assertEqual(sess_a["counts"]["rejected"], 2)
            self.assertEqual(sess_a["counts"]["attention"], 2)
            self.assertEqual(sess_a["counts"]["total"], 3)

    def test_cards_api_enriched_fields_and_counts(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        with isolated_hermes_home() as home:
            now_ts = time.time()
            _seed_enrichment_db(home / "kanban.db", now_ts)

            spec = importlib.util.spec_from_file_location(
                "factonio_test_plugin_api_enrich", PLUGIN_ROOT / "dashboard" / "plugin_api.py"
            )
            assert spec is not None and spec.loader is not None
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            app = FastAPI()
            app.include_router(mod.router, prefix="/api/plugins/factonio")
            client = TestClient(app)

            r_all = client.get("/api/plugins/factonio/cards")
            self.assertEqual(r_all.status_code, 200)
            self._assert_enriched(r_all.json(), now_ts)
            self._assert_all_counts(r_all.json()["counts"])

            r_b = client.get("/api/plugins/factonio/cards?session_id=sess_b")
            self.assertEqual(r_b.status_code, 200)
            body = r_b.json()
            self.assertEqual({c["id"] for c in body["cards"]}, {"t_vb_ok", "t_live", "t_blk"})
            self.assertEqual(body["counts"]["blocked"], 1)
            self.assertEqual(body["counts"]["rejected"], 0)
            self.assertEqual(body["counts"]["attention"], 1)
            self.assertEqual(body["counts"]["total"], 3)
            self.assertTrue(all(c["session_id"] == "sess_b" for c in body["cards"]))

            r_status = client.get("/api/plugins/factonio/status")
            self.assertEqual(r_status.status_code, 200)
            self.assertEqual(r_status.json()["counts"]["total"], 9)

    def test_thread_cards_minimal_legacy_schema_and_missing_db(self):
        from factonio import metrics

        with isolated_hermes_home() as home:
            missing = metrics.thread_cards(home / "nope.db")
            self.assertEqual(missing["cards"], [])
            self.assertEqual(missing["counts"]["total"], 0)
            self.assertEqual(missing["counts"]["attention"], 0)
            self.assertIn("todo", missing["counts"])

            now_ts = time.time()
            db_path = home / "legacy.db"
            conn = sqlite3.connect(db_path)
            conn.executescript(
                """
                CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT,
                    tenant TEXT, session_id TEXT, skills TEXT, model_override TEXT,
                    provider_override TEXT, block_kind TEXT, created_at REAL, started_at REAL,
                    completed_at REAL, result TEXT, body TEXT);
                CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
                    started_at REAL, ended_at REAL, outcome TEXT, metadata TEXT);
                """
            )
            conn.execute(
                "INSERT INTO tasks (id, title, assignee, status, tenant, session_id, skills, created_at, started_at, result)"
                " VALUES ('t_old', '[verify:diff] x/y#1', 'factonio', 'blocked', 'ws', 's1', ?, ?, ?, 'stuck')",
                (json.dumps(["factonio:verify-diff"]), now_ts - 100, now_ts - 100),
            )
            conn.execute(
                "INSERT INTO task_runs (task_id, started_at, ended_at, outcome, metadata) VALUES ('t_old', ?, ?, 'blocked', '{}')",
                (now_ts - 100, now_ts - 40),
            )
            conn.commit()
            conn.close()

            data = metrics.thread_cards(db_path)
            self.assertEqual(len(data["cards"]), 1)
            card = data["cards"][0]
            self.assertIsNone(card["summary"])
            self.assertIsNone(card["last_error"])
            self.assertIsNone(card["verdict"])
            self.assertEqual(card["last_outcome"], "blocked")
            self.assertAlmostEqual(card["active_duration_seconds"], 60, delta=1)
            self.assertEqual(data["counts"]["attention"], 1)
            self.assertEqual(data["counts"]["total"], 1)



def _seed_tri_state_db(db_path: Path, now_ts: float) -> None:
    """Verification cards with pass / reject / inconclusive verdicts (metadata and run outcome)."""
    from hermes_cli.kanban_db_connect import connect_closing

    rows = [
        # (id, title, status, skill, run outcome, metadata)
        ("t_d_pass", "[verify:diff] acme/api#1", "done", "factonio:verify-diff", "completed", {"verdict": "pass"}),
        # Done card with an explicit inconclusive verdict must NOT fall back to pass.
        ("t_d_inc", "[verify:diff] acme/api#2", "done", "factonio:verify-diff", "completed", {"verdict": "Inconclusive"}),
        # No metadata verdict, run outcome inconclusive.
        ("t_d_inc2", "[verify:diff] acme/api#3", "blocked", "factonio:verify-diff", "inconclusive", {}),
        ("t_b_pass", "[verify:blackbox] acme/api#1", "done", "factonio:verify-blackbox", "completed", {}),
        ("t_b_rej", "[verify:blackbox] acme/api#2", "done", "factonio:verify-blackbox", "completed", {"verdict": "reject"}),
        ("t_b_inc", "[verify:blackbox] acme/api#3", "done", "factonio:verify-blackbox", "completed",
         {"verdict": "inconclusive", "disclosed_limits": ["no browser"]}),
    ]
    with connect_closing(db_path=db_path) as conn:
        for i, (tid, title, status, skill, outcome, meta) in enumerate(rows):
            created = now_ts - 500 + i
            conn.execute(
                """
                INSERT INTO tasks (id, title, assignee, status, tenant, session_id, skills, created_at, started_at, completed_at)
                VALUES (?, ?, 'factonio', ?, 'ws-tri', 'sess_tri', ?, ?, ?, ?)
                """,
                (tid, title, status, json.dumps([skill]), created, created, created + 60 if status == "done" else None),
            )
            conn.execute(
                """
                INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome, metadata)
                VALUES (?, 'factonio', ?, ?, ?, ?, ?)
                """,
                (tid, outcome, created, created + 60, outcome, json.dumps(meta)),
            )
        conn.commit()


class TestTriStateVerdicts(unittest.TestCase):
    def _assert_metrics(self, m: dict) -> None:
        self.assertEqual(m["verification"]["diff"], {"pass": 1, "reject": 0, "inconclusive": 2, "pass_rate": 0.333})
        self.assertEqual(m["verification"]["blackbox"], {"pass": 1, "reject": 1, "inconclusive": 1, "pass_rate": 0.333})

    def _assert_cards(self, data: dict) -> None:
        verdicts = {c["id"]: c["verdict"] for c in data["cards"]}
        self.assertEqual(verdicts, {
            "t_d_pass": "pass", "t_d_inc": "inconclusive", "t_d_inc2": "inconclusive",
            "t_b_pass": "pass", "t_b_rej": "reject", "t_b_inc": "inconclusive",
        })
        counts = data["counts"]
        self.assertEqual(counts["inconclusive"], 3)
        self.assertEqual(counts["rejected"], 1)
        self.assertEqual(counts["blocked"], 1)
        self.assertEqual(counts["attention"], 1 + 1 + 3)  # blocked + rejected + inconclusive
        self.assertEqual(counts["total"], 6)

    def test_card_verdict_never_defaults_inconclusive_to_pass(self):
        from factonio.metrics import _card_verdict

        done = [{"outcome": "completed", "metadata": json.dumps({"verdict": "INCONCLUSIVE"})}]
        self.assertEqual(_card_verdict("verify-blackbox", "done", done), "inconclusive")
        self.assertEqual(_card_verdict("verify-diff", "done", [{"outcome": "inconclusive", "metadata": "{}"}]), "inconclusive")
        self.assertEqual(_card_verdict("verify-diff", "done", [{"outcome": "completed", "metadata": "{}"}]), "pass")
        self.assertEqual(_card_verdict("verify-diff", "ready", [{"outcome": "changes_requested"}]), "reject")
        self.assertIsNone(_card_verdict("verify-diff", "running", []))

    def test_metrics_and_thread_cards_count_inconclusive(self):
        from factonio import metrics

        with isolated_hermes_home() as home:
            empty = metrics.compute_metrics(home / "missing.db")
            self.assertEqual(empty["verification"]["diff"]["inconclusive"], 0)
            self.assertEqual(empty["verification"]["blackbox"]["inconclusive"], 0)
            self.assertEqual(metrics.thread_cards(home / "missing.db")["counts"]["inconclusive"], 0)

            now_ts = time.time()
            _seed_tri_state_db(home / "kanban.db", now_ts)
            self._assert_metrics(metrics.compute_metrics(home / "kanban.db", now=now_ts))
            self._assert_cards(metrics.thread_cards(home / "kanban.db"))

    def test_dashboard_api_passes_inconclusive_through(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        with isolated_hermes_home() as home:
            _seed_tri_state_db(home / "kanban.db", time.time())
            spec = importlib.util.spec_from_file_location(
                "factonio_test_plugin_api_tri", PLUGIN_ROOT / "dashboard" / "plugin_api.py"
            )
            assert spec is not None and spec.loader is not None
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            app = FastAPI()
            app.include_router(mod.router, prefix="/api/plugins/factonio")
            client = TestClient(app)

            r = client.get("/api/plugins/factonio/metrics?range=7d")
            self.assertEqual(r.status_code, 200)
            self._assert_metrics(r.json())
            r = client.get("/api/plugins/factonio/cards?tenant=ws-tri")
            self.assertEqual(r.status_code, 200)
            self._assert_cards(r.json())
            r = client.get("/api/plugins/factonio/status")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["counts"]["inconclusive"], 3)
            self.assertEqual(r.json()["counts"]["attention"], 5)



def _load_plugin_api(name: str):
    spec = importlib.util.spec_from_file_location(name, PLUGIN_ROOT / "dashboard" / "plugin_api.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _client(mod):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/factonio")
    return TestClient(app)


def _seed_mode_cards(db_path: Path, now_ts: float, report: Path) -> None:
    """Workstream cards stamped the way `orchestrator.start_workstream` stamps them."""
    from hermes_cli.kanban_db_connect import connect_closing

    from factonio.orchestrator import mode_contract_section

    def task(conn, tid, title, tenant, skill, created, body, status="done"):
        conn.execute(
            "INSERT INTO tasks (id, title, assignee, status, tenant, session_id, skills, created_at, body)"
            " VALUES (?, ?, 'factonio', ?, ?, 'sess_m', ?, ?, ?)",
            (tid, title, status, tenant, json.dumps([skill]), created, body),
        )

    with connect_closing(db_path=db_path) as conn:
        task(conn, "t_legacy", "[define] Old work", "ws-legacy", "factonio:define", now_ts - 900,
             "## Factonio Definition Card\n- Workstream tenant: `ws-legacy`\n")
        task(conn, "t_legacy_impl", "[implement] Old impl", "ws-legacy", "factonio:implement", now_ts - 800, "body")
        task(conn, "t_inv", "[define] [investigate] Why slow", "ws-inv", "factonio:define", now_ts - 700,
             f"- Mode: `investigate`\n- Investigation report path: `{report}`\n\n" + mode_contract_section(
                 "investigate", report_path=report))
        task(conn, "t_inv_tag", "[define] [investigate] Title only", "ws-inv-tag", "factonio:define", now_ts - 650,
             "No header lines here.\n")
        task(conn, "t_inv_hold", "[define] [investigate] Leak", "ws-inv-hold", "factonio:define", now_ts - 640,
             "- Mode: `investigate`\n- Investigation report path: `/runs/x/holdout/secret.md`\n")
        task(conn, "t_feat", "[define] Feature", "ws-feat", "factonio:define", now_ts - 600,
             "- Mode: `feature`\n- Investigation report path: `/tmp/should-not-leak.md`\n")
        task(conn, "t_refactor", "[implement] Tidy", "ws-refactor", "factonio:implement", now_ts - 500,
             "## Factonio Implementation Card\n\n" + mode_contract_section("refactor"))
        task(conn, "t_perf", "[implement] Speed", "ws-perf", "factonio:implement", now_ts - 400,
             "## Factonio Implementation Card\n\n" + mode_contract_section("perf"))
        task(conn, "t_trail", "[define] Trail wins", "ws-trail", "factonio:define", now_ts - 300, "- Mode: `feature`\n")
        conn.commit()


_LEGACY_MODE = {"kind": None, "read_only": False, "report_path": None, "report_exists": False}


class TestWorkstreamModes(unittest.TestCase):
    def test_parse_workstream_mode_sources_and_fallbacks(self):
        from factonio.metrics import parse_workstream_mode
        from factonio.orchestrator import mode_contract_section

        self.assertEqual(parse_workstream_mode({"body": "- Mode: `bug`\n"}), {"kind": "bug", "report_path": None})
        self.assertIsNone(parse_workstream_mode({"body": "- Mode: `chaos`\n", "title": "[define] x"}))
        self.assertIsNone(parse_workstream_mode({"body": "plain", "title": "[implement] x"}))
        self.assertEqual(parse_workstream_mode({"title": "[define] [investigate] X", "body": ""}),
                         {"kind": "investigate", "report_path": None})
        runs = [{"metadata": json.dumps({"report_path": "/r/investigation.md"})}]
        self.assertEqual(parse_workstream_mode({"title": "[define] [investigate] X", "body": ""}, runs)["report_path"],
                         "/r/investigation.md")
        self.assertEqual(parse_workstream_mode({"body": mode_contract_section("refactor")})["kind"], "refactor")
        self.assertEqual(parse_workstream_mode({"body": "## Behavior-Preservation Contract (refactor)\nx"})["kind"], "refactor")
        self.assertEqual(parse_workstream_mode({"body": mode_contract_section("perf")})["kind"], "perf")
        # A heading quoted mid-line is not a contract section.
        self.assertIsNone(parse_workstream_mode({"body": "see `## Performance Contract (benchmark-checklist)` docs"}))
        # report_path is only ever parsed for investigate.
        self.assertIsNone(parse_workstream_mode({"body": "- Mode: `feature`\n- Investigation report path: `/x`\n"},
                                                runs)["report_path"])

    def test_workstream_kinds_shared_constant(self):
        from factonio import config, orchestrator

        self.assertIs(orchestrator.WORKSTREAM_KINDS, config.WORKSTREAM_KINDS)
        self.assertEqual(config.WORKSTREAM_KINDS, ("feature", "bug", "quick", "refactor", "perf", "investigate"))

    def test_trail_modes_newest_valid_row_and_read_only(self):
        from factonio import trail

        with isolated_hermes_home() as home:
            db = trail.trail_db_path()
            self.assertEqual(trail.workstream_modes_from_trail(["ws"]), {})
            self.assertFalse(db.exists())
            trail.log_decision("start_workstream", tenant="ws", evidence={"kind": "perf"})
            trail.log_decision("start_workstream", tenant="ws", evidence={"kind": "bogus"})
            trail.log_decision("start_workstream", tenant="ws-inv", evidence={"kind": "investigate"})
            trail.log_decision("start_workstream", tenant="ws-old", evidence={"kind": "feature"})
            trail.log_decision("start_workstream", tenant="ws-old", evidence={"kind": "refactor", "read_only": True})
            trail.log_decision("merge", tenant="ws-x", evidence={"kind": "bug"})
            before = db.read_bytes()
            modes = trail.workstream_modes_from_trail(["ws", "ws-inv", "ws-old", "ws-x", "", "ws-none"])
            self.assertEqual(modes, {
                "ws": {"kind": "perf", "read_only": False},
                "ws-inv": {"kind": "investigate", "read_only": True},
                "ws-old": {"kind": "refactor", "read_only": True},
            })
            self.assertEqual(db.read_bytes(), before)
            self.assertEqual(trail.workstream_modes_from_trail([]), {})

            corrupt = home / "corrupt.db"
            corrupt.write_bytes(b"not sqlite" * 50)
            self.assertEqual(trail.workstream_modes_from_trail(["ws"], db_path=corrupt), {})
            self.assertEqual(trail.workstream_modes_from_trail(["ws"], db_path=home), {})
            missing = home / "nope" / "trail.db"
            self.assertEqual(trail.workstream_modes_from_trail(["ws"], db_path=missing), {})
            self.assertFalse(missing.parent.exists())

    def test_thread_cards_workstreams_resolution(self):
        from factonio import metrics, trail

        with isolated_hermes_home() as home:
            now_ts = time.time()
            report = home / "reports" / "investigation.md"
            report.parent.mkdir()
            report.write_text("# Root cause\n", encoding="utf-8")
            _seed_mode_cards(home / "kanban.db", now_ts, report)
            trail.log_decision("start_workstream", tenant="ws-trail", evidence={"kind": "perf", "read_only": False})
            runs_dir = home / "plugin-data" / "factonio" / "runs"

            data = metrics.thread_cards(home / "kanban.db", session_id="sess_m")
            ws = data["workstreams"]
            self.assertEqual(set(ws), {"ws-legacy", "ws-inv", "ws-inv-tag", "ws-inv-hold", "ws-feat", "ws-refactor",
                                       "ws-perf", "ws-trail"})
            self.assertEqual(ws["ws-legacy"], _LEGACY_MODE)
            self.assertEqual(ws["ws-inv"], {"kind": "investigate", "read_only": True, "report_path": str(report),
                                            "report_exists": True})
            fallback = runs_dir / "ws-inv-tag" / "investigation.md"
            self.assertEqual(ws["ws-inv-tag"], {"kind": "investigate", "read_only": True, "report_path": str(fallback),
                                                "report_exists": False})
            self.assertEqual(ws["ws-inv-hold"], {"kind": "investigate", "read_only": True, "report_path": None,
                                                 "report_exists": False})
            self.assertEqual(ws["ws-feat"], {"kind": "feature", "read_only": False, "report_path": None,
                                             "report_exists": False})
            self.assertEqual(ws["ws-refactor"]["kind"], "refactor")
            self.assertEqual(ws["ws-perf"]["kind"], "perf")
            self.assertEqual(ws["ws-trail"]["kind"], "perf")  # trail.db wins over the card header
            for t in ("ws-refactor", "ws-perf", "ws-trail"):
                self.assertEqual({k: ws[t][k] for k in ("read_only", "report_path", "report_exists")},
                                 {"read_only": False, "report_path": None, "report_exists": False})
            by_id = {c["id"]: c for c in data["cards"]}
            self.assertIsNone(by_id["t_legacy"]["workstream_kind"])
            self.assertIsNone(by_id["t_legacy_impl"]["workstream_kind"])
            self.assertEqual(by_id["t_inv"]["workstream_kind"], "investigate")
            self.assertEqual(by_id["t_trail"]["workstream_kind"], "perf")
            self.assertNotIn("holdout", json.dumps(data).lower())
            self.assertNotIn("should-not-leak", json.dumps(data))
            # Read-only: the fallback report path is computed, never created.
            self.assertFalse(runs_dir.exists())

            # An empty (but present) report is not ready.
            fallback.parent.mkdir(parents=True)
            fallback.write_text("", encoding="utf-8")
            self.assertFalse(metrics.thread_cards(home / "kanban.db", tenant="ws-inv-tag")["workstreams"]["ws-inv-tag"]
                             ["report_exists"])
            fallback.write_text("# Findings\n", encoding="utf-8")
            self.assertTrue(metrics.thread_cards(home / "kanban.db", tenant="ws-inv-tag")["workstreams"]["ws-inv-tag"]
                            ["report_exists"])

            # An explicitly queried tenant with no cards still gets a (legacy) entry.
            only = metrics.thread_cards(home / "kanban.db", tenant="ws-empty")
            self.assertEqual((only["cards"], only["workstreams"], only["arenas"]), ([], {"ws-empty": _LEGACY_MODE}, []))

    def test_cards_never_create_kanban_trail_or_runs(self):
        from factonio import metrics

        with isolated_hermes_home() as home:
            data_dir = home / "plugin-data"
            mod = _load_plugin_api("factonio_test_plugin_api_ro")
            client = _client(mod)
            for url in ("/api/plugins/factonio/cards", "/api/plugins/factonio/cards?tenant=ws-x",
                        "/api/plugins/factonio/cards?session_id=s1"):
                r = client.get(url)
                self.assertEqual(r.status_code, 200, r.text)
            body = client.get("/api/plugins/factonio/cards?tenant=ws-x").json()
            self.assertEqual(body["workstreams"], {"ws-x": _LEGACY_MODE})
            self.assertEqual(body["arenas"], [])
            direct = metrics.thread_cards(home / "kanban.db", tenant="ws-y")
            self.assertEqual(direct["workstreams"], {"ws-y": _LEGACY_MODE})
            self.assertFalse((home / "kanban.db").exists())
            self.assertFalse(data_dir.exists())

    def test_cards_api_exposes_workstreams_and_card_kinds(self):
        from factonio import trail

        with isolated_hermes_home() as home:
            now_ts = time.time()
            _seed_mode_cards(home / "kanban.db", now_ts, home / "nowhere.md")
            trail.log_decision("start_workstream", tenant="ws-legacy", evidence={"kind": "quick"})
            client = _client(_load_plugin_api("factonio_test_plugin_api_modes"))
            body = client.get("/api/plugins/factonio/cards?tenant=ws-legacy").json()
            self.assertEqual(body["workstreams"], {"ws-legacy": {"kind": "quick", "read_only": False, "report_path": None,
                                                                 "report_exists": False}})
            self.assertEqual({c["workstream_kind"] for c in body["cards"]}, {"quick"})
            inv = client.get("/api/plugins/factonio/cards?tenant=ws-inv").json()["workstreams"]["ws-inv"]
            self.assertEqual((inv["kind"], inv["report_exists"]), ("investigate", False))
            self.assertFalse((home / "plugin-data" / "factonio" / "runs").exists())


def _write_harness(repo: Path, manifest: object, *, raw: str | None = None) -> None:
    hdir = repo / ".factonio"
    hdir.mkdir(parents=True, exist_ok=True)
    (hdir / "verify.md").write_text("# Verify\n\n## Launch\n## Doctor\n## Drive\n## Evidence\n## Cleanup\n",
                                    encoding="utf-8")
    (hdir / "harness.json").write_text(raw if raw is not None else json.dumps(manifest), encoding="utf-8")


_HOOKS = [
    {"name": "unit", "kind": "custom", "command": "true"},
    {"name": "lint", "kind": "custom", "command": "true", "optional": True},
    {"name": "inv", "kind": "invariant", "command": "true", "covers": ["src/**"]},
    {"name": "shots", "kind": "visual_diff", "command": "true", "baseline_paths": ["docs/images/*.png"]},
    {"name": "loose", "kind": "visual_diff", "command": "true", "optional": True, "baseline_paths": ["a.png"],
     "frozen_baseline": False},
    {"name": "nobase", "kind": "visual_diff", "command": "true"},
]


class TestHarnessSummary(unittest.TestCase):
    _ABSENT = {"present": False, "mode": "absent", "required_hooks": 0, "optional_hooks": 0, "invariant_hooks": 0,
               "visual_diff_hooks": 0, "frozen_visual_hooks": 0, "has_manifest_error": False}

    def test_summary_shapes_and_malformed_manifests(self):
        from factonio.harness import summarize_harness_readonly

        with isolated_hermes_home() as home:
            for bad in (None, "", "   ", str(home / "missing"), home / "file.txt"):
                if isinstance(bad, Path):
                    bad.write_text("x", encoding="utf-8")
                with self.subTest(path=bad):
                    self.assertEqual(summarize_harness_readonly(bad), self._ABSENT)
            bare = home / "bare"
            bare.mkdir()
            self.assertEqual(summarize_harness_readonly(bare), self._ABSENT)

            repo = home / "repo"
            _write_harness(repo, {"schema_version": 1, "hooks": _HOOKS})
            self.assertEqual(summarize_harness_readonly(repo), {
                "present": True, "mode": "full", "required_hooks": 4, "optional_hooks": 2, "invariant_hooks": 1,
                "visual_diff_hooks": 3, "frozen_visual_hooks": 1, "has_manifest_error": False,
            })
            for label, manifest, raw in (
                ("invalid json", None, "{not json"),
                ("non-object", ["x"], None),
                ("schema_version", {"schema_version": 2, "hooks": []}, None),
                ("bool schema", {"schema_version": True, "hooks": []}, None),
                ("hooks not list", {"schema_version": 1, "hooks": "x"}, None),
                ("hooks null", {"schema_version": 1, "hooks": None}, None),
                ("hook not object", {"schema_version": 1, "hooks": [1, _HOOKS[0]]}, None),
            ):
                with self.subTest(label=label):
                    _write_harness(repo, manifest, raw=raw)
                    out = summarize_harness_readonly(repo)
                    self.assertTrue(out["has_manifest_error"])
                    self.assertEqual(set(out), set(self._ABSENT))
            _write_harness(repo, {"schema_version": 1})
            self.assertEqual(summarize_harness_readonly(repo)["has_manifest_error"], False)
            _write_harness(repo, {"schema_version": 1, "hooks": [_HOOKS[1]]})
            out = summarize_harness_readonly(repo)
            self.assertEqual((out["required_hooks"], out["optional_hooks"], out["has_manifest_error"]), (0, 1, False))

            only_md = home / "only-md"
            (only_md / ".factonio").mkdir(parents=True)
            (only_md / ".factonio" / "verify.md").write_text("# Verify\n", encoding="utf-8")
            self.assertEqual(summarize_harness_readonly(only_md)["mode"], "verify_md_only")

    def test_summary_runs_no_subprocess_and_touches_no_git_index(self):
        import subprocess
        from unittest import mock

        from factonio.harness import summarize_harness_readonly

        def boom(*a, **k):
            raise AssertionError("subprocess spawned")

        with isolated_hermes_home() as home:
            repo = home / "repo"
            (repo / ".git").mkdir(parents=True)
            _write_harness(repo, {"schema_version": 1, "hooks": _HOOKS})
            with mock.patch.object(subprocess, "run", boom), mock.patch.object(subprocess, "Popen", boom), \
                    mock.patch.object(subprocess, "check_output", boom):
                self.assertTrue(summarize_harness_readonly(repo)["present"])
                self.assertTrue(summarize_harness_readonly(PLUGIN_ROOT)["present"])
            self.assertEqual(sorted(p.name for p in (repo / ".git").iterdir()), [])

    def test_repo_own_harness_is_summarized(self):
        from factonio.harness import summarize_harness_readonly

        out = summarize_harness_readonly(PLUGIN_ROOT)
        self.assertEqual((out["present"], out["mode"], out["has_manifest_error"]), (True, "full", False))
        self.assertGreaterEqual(out["required_hooks"], 1)

    def test_repos_api_exposes_harness_and_patch_rejects_it(self):
        import subprocess
        from unittest import mock

        from factonio.repos import add_repo

        def boom(*a, **k):
            raise AssertionError("subprocess spawned")

        with isolated_hermes_home() as home:
            good = home / "dev" / "good"
            (good / ".git").mkdir(parents=True)
            _write_harness(good, {"schema_version": 1, "hooks": _HOOKS})
            bad = home / "dev" / "bad"
            (bad / ".git").mkdir(parents=True)
            _write_harness(bad, None, raw="{oops")
            plain = home / "dev" / "plain"
            (plain / ".git").mkdir(parents=True)
            nullhooks = home / "dev" / "nullhooks"
            (nullhooks / ".git").mkdir(parents=True)
            _write_harness(nullhooks, {"schema_version": 1, "hooks": None})
            for spec, path in (("acme/good", good), ("acme/bad", bad), ("acme/plain", plain),
                               ("acme/nullhooks", nullhooks)):
                add_repo(spec, local_path=str(path), clone=False, inspect_remote=False)
            api = _load_plugin_api("factonio_test_plugin_api_harness")
            client = _client(api)
            base = "/api/plugins/factonio"
            no_claude = {"claude_bin": None, "has_credentials": False, "available": False}
            mod = api._config
            with mock.patch.object(mod, "detect_claude_design_support", return_value=no_claude), \
                    mock.patch.object(subprocess, "run", boom), mock.patch.object(subprocess, "Popen", boom):
                r = client.get(f"{base}/repos")
                self.assertEqual(r.status_code, 200, r.text)
                by_slug = {item["slug"]: item["harness"] for item in r.json()["repos"]}
            self.assertEqual(by_slug["acme-good"]["frozen_visual_hooks"], 1)
            self.assertEqual(by_slug["acme-good"]["required_hooks"], 4)
            self.assertTrue(by_slug["acme-bad"]["has_manifest_error"])
            self.assertEqual((by_slug["acme-nullhooks"]["present"], by_slug["acme-nullhooks"]["has_manifest_error"]),
                             (True, True))
            self.assertEqual(by_slug["acme-plain"], TestHarnessSummary._ABSENT)
            for repo in (good, bad, plain):
                self.assertEqual(list((repo / ".git").iterdir()), [])

            store = home / "plugin-data" / "factonio" / "repos.json"
            before = store.read_bytes()
            for payload in ({"harness": {"present": True}}, {"merge_policy": "ask", "harness": None}):
                r = client.patch(f"{base}/repos/acme-good", json=payload)
                self.assertEqual(r.status_code, 422, r.text)
            self.assertEqual(store.read_bytes(), before)
            with mock.patch.object(mod, "detect_claude_design_support", return_value=no_claude):
                r = client.patch(f"{base}/repos/acme-good", json={"merge_policy": "ask"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["repo"]["harness"]["required_hooks"], 4)


def _seed_arena_cards(db_path: Path, now_ts: float, bodies: dict[str, str], tenant: str = "ws-arena") -> None:
    from hermes_cli.kanban_db_connect import connect_closing

    with connect_closing(db_path=db_path) as conn:
        for i, (tid, body) in enumerate(bodies.items()):
            conn.execute(
                "INSERT INTO tasks (id, title, assignee, status, tenant, session_id, skills, created_at, body)"
                " VALUES (?, ?, 'factonio', 'running', ?, 'sess_a', ?, ?, ?)",
                (tid, f"[define] Arena card {i}", tenant, json.dumps(["factonio:define"]), now_ts - 100 + i, body),
            )
        conn.commit()


def _write_arena_manifest(home: Path, tenant: str, arena_id: str, manifest: object, *, raw: str | None = None) -> None:
    adir = home / "plugin-data" / "factonio" / "runs" / tenant / "arena" / arena_id
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "manifest.json").write_text(raw if raw is not None else json.dumps(manifest), encoding="utf-8")


def _arena_stamp(aid: str, role: str, target: str, vendor: str, slot: str | None = None) -> str:
    slot_part = f", slot=`{slot}`" if slot else ""
    return f"## Card\n- Arena: `{aid}` (role=`{role}`, target=`{target}`{slot_part}, vendor=`{vendor}`)\n"


class TestArenaInformationBarrierApi(unittest.TestCase):
    def test_cards_never_leak_holdout_in_any_arena_field(self):
        with isolated_hermes_home() as home:
            aid = "ar_1111111111"
            _seed_arena_cards(home / "kanban.db", time.time(), {
                "t_a1": _arena_stamp(aid, "candidate", "define", "holdout-vendor", slot="c1-holdout-slot"),
                "t_a2": _arena_stamp(aid, "candidate", "define", "openai", slot="c2-openai"),
                "t_a3": _arena_stamp(aid, "judge", "define", "google/holdout/v"),
                "t_a4": _arena_stamp("ar_2222222222", "holdout", "holdout/x", "openai"),
                "t_a5": _arena_stamp("ar_2222222222", "reviewer", "build", "openai"),
            })
            _write_arena_manifest(home, "ws-arena", aid, {
                "arena_id": aid, "tenant": "ws-arena", "repo": "acme/holdout/repo", "target": "define",
                "title": "Copy holdout/scenario-1.md", "created_at_ns": 2,
                "candidates": [
                    {"slot": "c1-holdout-slot", "vendor": "holdout-vendor", "model": "holdout/model", "task_id": "t_a1"},
                    {"slot": "c2-openai", "vendor": "openai", "model": "gpt", "task_id": "t_a2"},
                    {"slot": "c3", "vendor": "x", "task_id": "t_holdout_gone"},
                ],
                "judge": {"task_id": "t_a3", "vendor": "holdout-judge", "model": "holdout/judge-model"},
            })
            for bad_id, manifest in (
                ("ar_3333333333", {"tenant": "holdout/ws", "target": "define"}),
                ("ar_4444444444", {"tenant": "other-ws", "target": "define"}),
                ("ar_5555555555", {"tenant": "ws-arena", "target": "holdout"}),
                ("ar_6666666666", {"tenant": "ws-arena", "target": "build"}),
            ):
                _write_arena_manifest(home, "ws-arena", bad_id, {"arena_id": bad_id, "created_at_ns": 1, **manifest})

            r = _client(_load_plugin_api("factonio_test_plugin_api_arena_barrier")).get(
                "/api/plugins/factonio/cards?tenant=ws-arena")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertNotIn("holdout", r.text.lower())
            body = r.json()
            self.assertEqual([a["arena_id"] for a in body["arenas"]], [aid])
            arena = body["arenas"][0]
            self.assertEqual((arena["tenant"], arena["repo"], arena["target"], arena["title"]),
                             ("ws-arena", None, "define", None))
            self.assertEqual([{k: c[k] for k in ("slot", "vendor", "model", "task_id")} for c in arena["candidates"]], [
                {"slot": None, "vendor": None, "model": None, "task_id": "t_a1"},
                {"slot": "c2-openai", "vendor": "openai", "model": "gpt", "task_id": "t_a2"},
                {"slot": "c3", "vendor": "x", "model": None, "task_id": None},
            ])
            self.assertEqual((arena["judge"]["task_id"], arena["judge"]["vendor"], arena["judge"]["model"]),
                             ("t_a3", None, None))
            keys = ("arena_id", "arena_role", "arena_slot", "arena_vendor", "arena_target")
            by_id = {c["id"]: {k: c[k] for k in keys} for c in body["cards"]}
            self.assertEqual(by_id, {
                "t_a1": {"arena_id": aid, "arena_role": "candidate", "arena_slot": None, "arena_vendor": None,
                         "arena_target": "define"},
                "t_a2": {"arena_id": aid, "arena_role": "candidate", "arena_slot": "c2-openai", "arena_vendor": "openai",
                         "arena_target": "define"},
                "t_a3": {"arena_id": aid, "arena_role": "judge", "arena_slot": None, "arena_vendor": None,
                         "arena_target": "define"},
                "t_a4": {"arena_id": "ar_2222222222", "arena_role": None, "arena_slot": None, "arena_vendor": "openai",
                         "arena_target": None},
                "t_a5": {"arena_id": "ar_2222222222", "arena_role": None, "arena_slot": None, "arena_vendor": "openai",
                         "arena_target": None},
            })

    def test_cards_skip_non_finite_arena_timestamp_and_keep_valid_sibling(self):
        from factonio.arena import _created_ns

        self.assertEqual(_created_ns({"created_at_ns": float("inf")}), 0)
        self.assertEqual(_created_ns({"created_at_ns": float("nan"), "created_at": 3}), 3_000_000_000)
        self.assertEqual(_created_ns({"created_at_ns": True}), 0)
        with isolated_hermes_home() as home:
            _seed_arena_cards(home / "kanban.db", time.time(), {"t_c1": "plain body"})
            good = {"arena_id": "ar_aaaaaaaaaa", "tenant": "ws-arena", "target": "define", "title": "Good",
                    "created_at_ns": 5_000_000_000, "candidates": []}
            _write_arena_manifest(home, "ws-arena", "ar_aaaaaaaaaa", good)
            _write_arena_manifest(home, "ws-arena", "ar_bbbbbbbbbb", None, raw=(
                '{"arena_id": "ar_bbbbbbbbbb", "tenant": "ws-arena", "target": "define", "created_at_ns": 1e309}'))
            _write_arena_manifest(home, "ws-arena", "ar_cccccccccc", None, raw=(
                '{"arena_id": "ar_cccccccccc", "tenant": "ws-arena", "target": "define", "created_at": -1e309}'))
            r = _client(_load_plugin_api("factonio_test_plugin_api_arena_inf")).get(
                "/api/plugins/factonio/cards?tenant=ws-arena")
            self.assertEqual(r.status_code, 200, r.text)
            arenas = r.json()["arenas"]
            self.assertEqual([(a["arena_id"], a["title"], a["created_at"]) for a in arenas], [("ar_aaaaaaaaaa", "Good", 5)])

    def test_cards_judge_cross_vendor_is_strict_bool_or_null(self):
        cases = (
            ('"/runs/probe-ws/holdout/private-scenario.md"', None),
            ('{"nested": "holdout/secret.md"}', None),
            ('["holdout/a.md"]', None),
            ("1e309", None),
            ("1", None),
            ('"true"', None),
            ("true", True),
            ("false", False),
            ("null", None),
        )
        for raw_cv, expected in cases:
            with self.subTest(cross_vendor=raw_cv), isolated_hermes_home() as home:
                aid = "ar_dddddddddd"
                _seed_arena_cards(home / "kanban.db", time.time(), {
                    "t_j1": _arena_stamp(aid, "judge", "define", "google"),
                }, tenant="ws-cv")
                _write_arena_manifest(home, "ws-cv", aid, None, raw=(
                    '{"arena_id": "ar_dddddddddd", "tenant": "ws-cv", "target": "define", "created_at_ns": 7,'
                    ' "candidates": [], "judge": {"task_id": "t_j1", "vendor": "google", "model": "gemini",'
                    f' "cross_vendor": {raw_cv}}}}}'))
                r = _client(_load_plugin_api("factonio_test_plugin_api_arena_cv")).get(
                    "/api/plugins/factonio/cards?tenant=ws-cv")
                self.assertEqual(r.status_code, 200, r.text)
                self.assertNotIn("holdout", r.text.lower())
                judge = r.json()["arenas"][0]["judge"]
                self.assertEqual(judge, {"task_id": "t_j1", "status": "running", "vendor": "google",
                                         "model": "gemini", "cross_vendor": expected})
                self.assertIs(type(judge["cross_vendor"]), type(expected))

    def test_cards_arena_fields_reject_non_string_manifest_values(self):
        with isolated_hermes_home() as home:
            aid = "ar_eeeeeeeeee"
            _seed_arena_cards(home / "kanban.db", time.time(), {
                "t_c1": _arena_stamp(aid, "candidate", "define", "openai", slot="c1"),
                "t_j1": _arena_stamp(aid, "judge", "define", "google"),
            }, tenant="ws-typed")
            _write_arena_manifest(home, "ws-typed", aid, None, raw=(
                '{"arena_id": "ar_eeeeeeeeee", "tenant": "ws-typed", "target": "define", "created_at_ns": 9e9,'
                ' "repo": {"p": "holdout/r"}, "title": 1e309,'
                ' "candidates": [{"slot": ["holdout"], "vendor": 1e309, "model": {"x": "holdout/m"}, "task_id": "t_c1"}],'
                ' "judge": {"task_id": "t_j1", "vendor": ["holdout/v"], "label": 1e309, "cross_vendor": true}}'))
            r = _client(_load_plugin_api("factonio_test_plugin_api_arena_typed")).get(
                "/api/plugins/factonio/cards?tenant=ws-typed")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertNotIn("holdout", r.text.lower())
            arena = r.json()["arenas"][0]
            self.assertEqual((arena["repo"], arena["title"], arena["created_at"]), (None, None, 9))
            self.assertEqual(arena["candidates"], [{"slot": None, "vendor": None, "model": None, "task_id": "t_c1",
                                                    "status": "running", "artifact_ready": False}])
            self.assertEqual(arena["judge"], {"task_id": "t_j1", "status": "running", "vendor": None, "model": None,
                                              "cross_vendor": True})
            for key in ("ready_for_synthesis", "synthesized", "canonical_artifact_exists"):
                self.assertIs(type(arena[key]), bool)
            self.assertTrue(all(isinstance(a, str) for a in arena["advisories"]))


class TestTrailInformationBarrierApi(unittest.TestCase):
    def test_trail_list_and_detail_scrub_holdout_keys_paths_counts_and_text(self):
        from factonio import trail

        with isolated_hermes_home():
            leak = trail.log_decision(
                "arena_synthesis", tenant="ws-t", phase="define", actor="judge",
                why="Judge read holdout/scenario-1.md before synthesis",
                result="merged spec; see runs/ws-t/holdout/",
                evidence={
                    "holdout_path": "/runs/ws-t/holdout", "holdout_files": 3, "HoldOut_Count": 2,
                    "kind": "define", "winner": "c1",
                    "nested": {"note": "copied holdout/b.md", "items": ["ok", "x/holdout/b", 7],
                               "holdout_dir": "/x"},
                },
            )
            normal = trail.log_decision("merge", tenant="ws-t", phase="merge", actor="orchestrator", why="CI green",
                                        evidence={"pr": 7, "checks": ["unit", "lint"]})
            client = _client(_load_plugin_api("factonio_test_plugin_api_trail_barrier"))
            base = "/api/plugins/factonio"
            listing = client.get(f"{base}/trail?tenant=ws-t")
            detail = client.get(f"{base}/trail/{leak['id']}")
            for r in (listing, detail):
                self.assertEqual(r.status_code, 200, r.text)
                self.assertNotIn("holdout", r.text.lower())
                self.assertNotIn("scenario-1", r.text)
            row = detail.json()["decision"]
            self.assertEqual(row["evidence"], {"kind": "define", "winner": "c1",
                                               "nested": {"note": "[redacted]", "items": ["ok", "[redacted]", 7]}})
            self.assertEqual((row["why"], row["result"], row["decision"], row["tenant"]),
                             ("[redacted]", "[redacted]", "arena_synthesis", "ws-t"))
            by_id = {d["id"]: d for d in listing.json()["decisions"]}
            self.assertEqual(by_id[leak["id"]], row)
            self.assertEqual(by_id[normal["id"]], trail.get_decision(normal["id"]))
            self.assertEqual(client.get(f"{base}/trail/{normal['id']}").json()["decision"],
                             trail.get_decision(normal["id"]))


# --- Interactive control-plane API (publish-pr, report, harness audit, panel/arena evaluate) -------

_CP = "/api/plugins/factonio"
_SHA = "ab" * 20


def _cp_client(name: str):
    return _client(_load_plugin_api(name))


def _cp_register(home: Path, *, git_repo: bool = False) -> Path:
    from tests.test_local_publish_gate import _register

    return _register(home, git_repo=git_repo)


def _cp_complete(task_id: str, verdict, **meta) -> None:
    from tests.test_local_publish_gate import _complete

    _complete(task_id, verdict, **meta)


def _trail_rows(decision: str) -> list[dict]:
    from factonio.trail import query_decisions

    return [r for r in query_decisions(limit=1000) if r["decision"] == decision]


class TestLocalBranchVerificationStatus(unittest.TestCase):
    def test_metadata_prefill_is_scrubbed(self):
        from factonio.metrics import _local_target_from_metadata

        base = {"branch": "feat/y", "head_sha": _SHA, "repo": "acme/svc"}
        got = _local_target_from_metadata({**base, "pr_title": "Add y", "pr_body": "## Why\ny"})
        self.assertEqual((got["pr_title"], got["pr_body"]), ("Add y", "## Why\ny"))
        for title, body in (("", "  "), (7, ["x"]), ("see holdout/s1.md", "copied hold-out/x"), (None, None)):
            with self.subTest(title=title):
                got = _local_target_from_metadata({**base, "pr_title": title, "pr_body": body})
                self.assertNotIn("pr_title", got)
                self.assertNotIn("pr_body", got)

    def test_standard_pair_states(self):
        from factonio.metrics import local_branch_verification_status as status
        from factonio.orchestrator import spawn_verification_pair

        with isolated_hermes_home() as home:
            db = home / "kanban.db"
            missing = status(home / "nope.db", repo="acme/svc", branch="feat/x", head_sha=_SHA)
            self.assertEqual((missing["verified"], missing["mode"]), (False, "none"))
            self.assertFalse((home / "nope.db").exists())
            _cp_register(home)
            self.assertFalse(status(db, repo="acme/svc", branch="feat/x", head_sha=_SHA)["verified"])
            pair = spawn_verification_pair("acme/svc", "ws-loc", branch="feat/x", head_sha=_SHA)
            pending = status(db, repo="acme/svc", branch="feat/x", head_sha=_SHA)
            self.assertEqual((pending["verified"], pending["mode"], pending["consensus"]), (False, "standard", "pending"))
            self.assertEqual(len(pending["lanes"]), 2)
            _cp_complete(pair["verify_diff_task_id"], "pass")
            _cp_complete(pair["verify_blackbox_task_id"], None)
            self.assertFalse(status(db, repo="acme/svc", branch="feat/x", head_sha=_SHA)["verified"],
                             "a done card without an explicit verdict never counts as a pass")
            again = spawn_verification_pair("acme/svc", "ws-loc", branch="feat/x", head_sha=_SHA)
            _cp_complete(again["verify_diff_task_id"], "pass")
            _cp_complete(again["verify_blackbox_task_id"], "pass")
            self.assertFalse(status(db, repo="acme/svc", branch="feat/x", head_sha=_SHA)["verified"])

        with isolated_hermes_home() as home:
            db = home / "kanban.db"
            _cp_register(home)
            pair = spawn_verification_pair("acme/svc", "ws-loc", branch="feat/x", head_sha=_SHA)
            _cp_complete(pair["verify_diff_task_id"], "pass")
            _cp_complete(pair["verify_blackbox_task_id"], "pass")
            ok = status(db, repo="acme/svc", branch="feat/x", head_sha=_SHA.upper())
            self.assertEqual((ok["verified"], ok["mode"], ok["consensus"], ok["base_branch"]),
                             (True, "standard", "pass", "main"))
            self.assertTrue(status(db, repo="ACME/svc", branch="feat/x", head_sha=_SHA, tenant="ws-loc")["verified"])
            for kwargs in ({"head_sha": "cd" * 20}, {"branch": "feat/x2"}, {"repo": "acme/other"},
                           {"tenant": "ws-other"}, {"head_sha": _SHA[:12]}):
                with self.subTest(kwargs=kwargs):
                    args = {"repo": "acme/svc", "branch": "feat/x", "head_sha": _SHA, **kwargs}
                    self.assertFalse(status(db, **args)["verified"])
            reject = spawn_verification_pair("acme/svc", "ws-loc", branch="feat/x", head_sha=_SHA)
            _cp_complete(reject["verify_blackbox_task_id"], "reject", findings=["boom"])
            rej = status(db, repo="acme/svc", branch="feat/x", head_sha=_SHA)
            self.assertEqual((rej["verified"], rej["consensus"]), (False, "reject"))

    def test_panel_round(self):
        from factonio.metrics import local_branch_verification_status as status
        from factonio.orchestrator import spawn_verification_pair
        from tests.test_local_publish_gate import TWO_VENDORS

        with isolated_hermes_home() as home:
            db = home / "kanban.db"
            _cp_register(home)
            loc = spawn_verification_pair("acme/svc", "ws-p", branch="feat/p", head_sha=_SHA, rigor="high",
                                          panel_models=TWO_VENDORS)
            lanes = loc["panel"]["lanes"]
            for lane in lanes[:1]:
                _cp_complete(lane["verify_diff_task_id"], "pass")
                _cp_complete(lane["verify_blackbox_task_id"], "pass")
            partial = status(db, repo="acme/svc", branch="feat/p", head_sha=_SHA)
            self.assertEqual((partial["verified"], partial["mode"], partial["consensus"]), (False, "panel", "pending"))
            self.assertEqual(partial["panel_id"], loc["panel"]["panel_id"])
            for lane in lanes[1:]:
                _cp_complete(lane["verify_diff_task_id"], "pass")
                _cp_complete(lane["verify_blackbox_task_id"], "pass")
            ok = status(db, repo="acme/svc", branch="feat/p", head_sha=_SHA)
            self.assertEqual((ok["verified"], ok["consensus"], len(ok["lanes"])), (True, "pass", 4))
            # Archiving a spawned lane shrinks the panel: the recorded roster makes it fail closed.
            from hermes_cli.kanban_db_connect import connect_closing

            with connect_closing(db_path=db) as conn:
                conn.execute("UPDATE tasks SET status = 'archived' WHERE id = ?", (lanes[1]["verify_diff_task_id"],))
                conn.commit()
            shrunk = status(db, repo="acme/svc", branch="feat/p", head_sha=_SHA)
            self.assertEqual((shrunk["verified"], shrunk["consensus"]), (False, "inconclusive"))


class TestPublishPrApi(unittest.TestCase):
    OK = {"repo": "acme/svc", "branch": "feat/x", "expected_head_sha": _SHA, "title": "Add x", "confirmed": True}

    def _verified(self, home: Path, sha: str = _SHA) -> None:
        from factonio.orchestrator import spawn_verification_pair

        pair = spawn_verification_pair("acme/svc", "ws-pub", branch="feat/x", head_sha=sha)
        _cp_complete(pair["verify_diff_task_id"], "pass")
        _cp_complete(pair["verify_blackbox_task_id"], "pass")

    def test_strict_validation_and_unregistered(self):
        from unittest import mock

        with isolated_hermes_home() as home:
            _cp_register(home)
            mod = _load_plugin_api("factonio_test_cp_publish_validation")
            client = _client(mod)
            bad = [
                None, [], "x",
                {**self.OK, "confirmed": False}, {**self.OK, "confirmed": "true"}, {**self.OK, "confirmed": 1},
                {k: v for k, v in self.OK.items() if k != "confirmed"},
                {**self.OK, "body_file": "/etc/passwd"}, {**self.OK, "remote": "evil"}, {**self.OK, "actor": "human"},
                {**self.OK, "expected_head_sha": _SHA[:12]}, {**self.OK, "expected_head_sha": "g" * 40},
                {**self.OK, "expected_head_sha": _SHA + "0"}, {**self.OK, "expected_head_sha": 7},
                {**self.OK, "title": ""}, {**self.OK, "title": "   "}, {**self.OK, "title": "a\nb"},
                {**self.OK, "title": None}, {**self.OK, "branch": True}, {**self.OK, "branch": 12},
                {**self.OK, "branch": {"x": 1}}, {**self.OK, "repo": ["acme/svc"]}, {**self.OK, "draft": "true"},
                {**self.OK, "draft": 1}, {**self.OK, "draft": None}, {**self.OK, "body": 5},
                {**self.OK, "body": "x\x00"}, {**self.OK, "base_branch": False}, {**self.OK, "tenant": "../ws"},
                {**self.OK, "tenant": "ws/a"}, {**self.OK, "card_id": 3},
            ]
            with mock.patch.object(mod._repos, "publish_pr", side_effect=AssertionError("must not publish")):
                for payload in bad:
                    with self.subTest(payload=payload):
                        r = client.post(f"{_CP}/publish-pr", json=payload)
                        self.assertEqual(r.status_code, 422, r.text)
                r = client.post(f"{_CP}/publish-pr", json={**self.OK, "repo": "acme/unknown"})
                self.assertEqual(r.status_code, 404, r.text)
            self.assertEqual(_trail_rows("publish_pr"), [])

    def test_unverified_refuses_without_git_or_gh(self):
        import subprocess as sp
        from unittest import mock

        with isolated_hermes_home() as home:
            _cp_register(home)
            mod = _load_plugin_api("factonio_test_cp_publish_unverified")
            client = _client(mod)
            with mock.patch.object(mod._repos, "publish_pr", side_effect=AssertionError("must not publish")), \
                    mock.patch.object(sp, "run", side_effect=AssertionError("no subprocess")), \
                    mock.patch.object(sp, "Popen", side_effect=AssertionError("no subprocess")):
                r = client.post(f"{_CP}/publish-pr", json=self.OK)
            self.assertEqual(r.status_code, 409, r.text)
            detail = r.json()["detail"]
            self.assertEqual({k: detail[k] for k in ("ok", "published", "pushed", "created", "refused")},
                             {"ok": False, "published": False, "pushed": False, "created": False, "refused": True})
            self.assertFalse(detail["verification"]["verified"])
            rows = _trail_rows("publish_pr")
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0]["actor"], rows[0]["result"], rows[0]["phase"]), ("human", "refused", "publish"))
            self.assertEqual(rows[0]["evidence"]["expected_head_sha"], _SHA)
            self.assertFalse(rows[0]["evidence"]["gate"]["pushed"])

            self._verified(home)
            with mock.patch.object(mod._repos, "publish_pr", side_effect=AssertionError("must not publish")):
                drifted = client.post(f"{_CP}/publish-pr", json={**self.OK, "expected_head_sha": "cd" * 20})
                wrong_base = client.post(f"{_CP}/publish-pr", json={**self.OK, "base_branch": "develop"})
            self.assertEqual((drifted.status_code, wrong_base.status_code), (409, 409))
            self.assertEqual(len(_trail_rows("publish_pr")), 3)

    def test_verified_publish_maps_outcomes(self):
        from unittest import mock

        with isolated_hermes_home() as home:
            _cp_register(home)
            self._verified(home)
            mod = _load_plugin_api("factonio_test_cp_publish_verified")
            client = _client(mod)
            published = {"ok": True, "published": True, "refused": False, "pr_number": 12,
                         "url": "https://github.com/acme/svc/pull/12", "head_sha": _SHA}
            with mock.patch.object(mod._repos, "publish_pr", return_value=published) as pub:
                r = client.post(f"{_CP}/publish-pr", json={**self.OK, "expected_head_sha": _SHA.upper(),
                                                           "body": "## Why\nx", "draft": True, "tenant": "ws-pub"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual((r.json()["pr_number"], r.json()["verification"]["mode"]), (12, "standard"))
            args, kwargs = pub.call_args
            self.assertEqual(args, ("acme/svc", "feat/x"))
            self.assertEqual(
                {k: kwargs[k] for k in ("expected_head_sha", "remote", "actor", "base_branch", "draft", "body", "tenant")},
                {"expected_head_sha": _SHA, "remote": "origin", "actor": "human", "base_branch": "main",
                 "draft": True, "body": "## Why\nx", "tenant": "ws-pub"},
            )
            self.assertNotIn("body_file", kwargs)
            for res, code in (({"ok": False, "refused": True, "error": "HEAD drift"}, 409),
                              ({"ok": False, "refused": False, "pushed": True, "error": "gh pr create failed"}, 502)):
                with self.subTest(code=code), mock.patch.object(mod._repos, "publish_pr", return_value=res):
                    self.assertEqual(client.post(f"{_CP}/publish-pr", json=self.OK).status_code, code)

    def test_real_head_drift_is_refused_before_push(self):
        from tests.test_local_publish_gate import _git, _git_env

        with isolated_hermes_home() as home, _git_env():
            repo = _cp_register(home, git_repo=True)
            _git(repo, "checkout", "-q", "-b", "feat/x")
            (repo / "new.txt").write_text("x\n", encoding="utf-8")
            _git(repo, "add", "new.txt")
            _git(repo, "commit", "-q", "-m", "feat")
            head = _git(repo, "rev-parse", "HEAD")
            self._verified(home, sha=head)
            (repo / "new.txt").write_text("y\n", encoding="utf-8")
            _git(repo, "commit", "-q", "-am", "moved after verification")
            client = _cp_client("factonio_test_cp_publish_drift")
            r = client.post(f"{_CP}/publish-pr", json={**self.OK, "expected_head_sha": head})
            self.assertEqual(r.status_code, 409, r.text)
            self.assertIn("drift", r.json()["detail"]["error"].lower())
            self.assertFalse(r.json()["detail"]["pushed"])
            self.assertIs(r.json()["detail"]["ok"], False)
            self.assertIs(r.json()["detail"]["refused"], True)
            row = _trail_rows("publish_pr")[0]
            self.assertEqual((row["actor"], row["result"]), ("human", "refused"))


class TestInvestigationReportApi(unittest.TestCase):
    def _runs(self, home: Path) -> Path:
        return home / "plugin-data" / "factonio" / "runs"

    def test_absent_present_truncated_and_scrubbed(self):
        with isolated_hermes_home() as home:
            client = _cp_client("factonio_test_cp_report")
            r = client.get(f"{_CP}/workstreams/ws-inv/report")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json(), {"ok": True, "exists": False, "tenant": "ws-inv", "filename": "investigation.md",
                                        "content": None, "updated_at": None, "size_bytes": 0, "truncated": False,
                                        "redacted": False})
            self.assertFalse(self._runs(home).exists(), "the viewer never creates runs/ directories")
            run = self._runs(home) / "ws-inv"
            run.mkdir(parents=True)
            (run / "investigation.md").write_text("# Findings\n\nSlow query in `db.py`.\n", encoding="utf-8")
            body = client.get(f"{_CP}/workstreams/ws-inv/report").json()
            self.assertEqual((body["exists"], body["content"], body["truncated"], body["redacted"]),
                             (True, "# Findings\n\nSlow query in `db.py`.\n", False, False))
            self.assertTrue(body["updated_at"].endswith("Z"))
            self.assertEqual(body["size_bytes"], len("# Findings\n\nSlow query in `db.py`.\n"))
            # Mixed-case tenants resolve to the same safe run directory the orchestrator writes.
            self.assertEqual(client.get(f"{_CP}/workstreams/WS-Inv/report").json()["content"], body["content"])
            (run / "investigation.md").write_text("ok line\nsee holdout/scenario-1.md\nend\n", encoding="utf-8")
            scrubbed = client.get(f"{_CP}/workstreams/ws-inv/report")
            self.assertNotIn("holdout", scrubbed.text.lower())
            self.assertNotIn("scenario-1", scrubbed.text)
            self.assertEqual((scrubbed.json()["content"], scrubbed.json()["redacted"]), ("ok line\n[redacted]\nend\n", True))
            (run / "investigation.md").write_text("a hol\ndout/x\n", encoding="utf-8")
            self.assertEqual(client.get(f"{_CP}/workstreams/ws-inv/report").json()["content"], "[redacted]")
            big = "x" * (256 * 1024 + 10)
            (run / "investigation.md").write_text(big, encoding="utf-8")
            capped = client.get(f"{_CP}/workstreams/ws-inv/report").json()
            self.assertEqual((len(capped["content"]), capped["truncated"], capped["size_bytes"]),
                             (256 * 1024, True, len(big)))
            for resp in (r, scrubbed, client.get(f"{_CP}/workstreams/ws-inv/report")):
                self.assertNotIn(str(home), resp.text)
            (run / "investigation.md").write_bytes(b"PK\x00\x01binary")
            self.assertEqual(client.get(f"{_CP}/workstreams/ws-inv/report").status_code, 422)

    def test_tenant_validation_and_confinement(self):
        import os as _os

        with isolated_hermes_home() as home:
            client = _cp_client("factonio_test_cp_report_confine")
            for tenant in ("..", ".hidden", "-x", "a..b", "holdout", "ws-holdout-1", "a%00b", "a%0Ab", "a b",
                           "x" * 129, "%2E%2E", "ws%5Cx", "ws~1"):
                with self.subTest(tenant=tenant):
                    r = client.get(f"{_CP}/workstreams/{tenant}/report")
                    self.assertIn(r.status_code, (404, 422), r.text)
                    self.assertNotEqual(r.status_code, 200)
            self.assertEqual(client.get(f"{_CP}/workstreams/a%2Fb/report").status_code, 404)
            outside = home / "secret.md"
            outside.write_text("TOP SECRET\n", encoding="utf-8")
            runs = self._runs(home)
            (runs / "ws-link").mkdir(parents=True)
            (runs / "ws-link" / "investigation.md").symlink_to(outside)
            (runs / "ws-dirlink").symlink_to(home)
            (home / "investigation.md").write_text("ESCAPED\n", encoding="utf-8")
            (runs / "ws-dir" / "investigation.md").mkdir(parents=True)
            (runs / "ws-fifo").mkdir()
            _os.mkfifo(runs / "ws-fifo" / "investigation.md")
            for tenant in ("ws-link", "ws-dirlink", "ws-dir", "ws-fifo"):
                with self.subTest(tenant=tenant):
                    r = client.get(f"{_CP}/workstreams/{tenant}/report")
                    self.assertEqual(r.status_code, 403, r.text)
                    self.assertNotIn("SECRET", r.text)
                    self.assertNotIn("ESCAPED", r.text)


    def test_symlinked_ancestors_and_mid_walk_swap_fail_closed(self):
        import os as _os
        import shutil
        from unittest import mock

        with isolated_hermes_home() as home:
            client = _cp_client("factonio_test_cp_report_dirfd")
            outside = home / "outside"
            (outside / "ws-a").mkdir(parents=True)
            (outside / "ws-a" / "investigation.md").write_text("OUTSIDE SECRET\n", encoding="utf-8")
            runs = self._runs(home)
            runs.parent.mkdir(parents=True)
            runs.symlink_to(outside, target_is_directory=True)
            r = client.get(f"{_CP}/workstreams/ws-a/report")
            self.assertEqual(r.status_code, 403, r.text)
            self.assertNotIn("SECRET", r.text)
            runs.unlink()
            fdata = home / "plugin-data" / "factonio"
            shutil.move(str(fdata), str(home / "fdata"))
            fdata.symlink_to(home / "fdata", target_is_directory=True)
            (home / "fdata" / "runs").symlink_to(outside, target_is_directory=True)
            self.assertEqual(client.get(f"{_CP}/workstreams/ws-a/report").status_code, 403)
            fdata.unlink()
            runs.mkdir(parents=True)
            self.assertEqual(client.get(f"{_CP}/workstreams/ws-a/report").json()["exists"], False)
            (runs / "ws-a").symlink_to(outside / "ws-a", target_is_directory=True)
            r = client.get(f"{_CP}/workstreams/ws-a/report")
            self.assertEqual(r.status_code, 403, r.text)
            self.assertNotIn("SECRET", r.text)
            (runs / "ws-a").unlink()
            (runs / "ws-a").mkdir()
            (runs / "ws-a" / "investigation.md").write_text("inside\n", encoding="utf-8")
            self.assertEqual(client.get(f"{_CP}/workstreams/ws-a/report").json()["content"], "inside\n")

            mod = _load_plugin_api("factonio_test_cp_report_swap")
            real_open = _os.open

            def swapping_open(path, flags, *args, **kwargs):
                if path == "investigation.md":
                    _os.rename(runs / "ws-a", runs / "ws-a.old")
                    (runs / "ws-a").symlink_to(outside / "ws-a", target_is_directory=True)
                return real_open(path, flags, *args, **kwargs)

            with mock.patch.object(mod.os, "open", side_effect=swapping_open):
                r = _client(mod).get(f"{_CP}/workstreams/ws-a/report")
            self.assertEqual(r.status_code, 403, r.text)
            self.assertNotIn("SECRET", r.text)
            self.assertNotIn("inside", r.text)

    def test_utf8_cap_path_redaction_and_post_redaction_cap(self):
        cap = 256 * 1024
        with isolated_hermes_home() as home:
            client = _cp_client("factonio_test_cp_report_utf8")
            run = self._runs(home) / "ws-u"
            run.mkdir(parents=True)
            report = run / "investigation.md"
            for ch in ("\u00e9", "\u65e5", "\U0001F680"):
                for pad in range(1, len(ch.encode("utf-8"))):
                    with self.subTest(ch=ch, pad=pad):
                        report.write_text("a" * (cap - pad) + ch * 3, encoding="utf-8")
                        body = client.get(f"{_CP}/workstreams/ws-u/report").json()
                        self.assertTrue(body["truncated"])
                        self.assertLessEqual(len(body["content"].encode("utf-8")), cap)
                        self.assertNotIn("\ufffd", body["content"])
                        self.assertEqual(body["content"], "a" * (cap - pad))
            report.write_text("\u65e5\u672c" * 60000, encoding="utf-8")
            body = client.get(f"{_CP}/workstreams/ws-u/report").json()
            self.assertEqual((body["truncated"], len(body["content"].encode("utf-8"))), (True, cap - 1))
            self.assertNotIn("\ufffd", body["content"])

            report.write_text("holdout\n" * 45000, encoding="utf-8")
            body = client.get(f"{_CP}/workstreams/ws-u/report").json()
            self.assertEqual((body["truncated"], body["redacted"]), (True, True))
            self.assertLessEqual(len(body["content"].encode("utf-8")), cap)
            self.assertNotIn("holdout", body["content"])

            report.write_text("/tmp/x\n" * 50000, encoding="utf-8")
            body = client.get(f"{_CP}/workstreams/ws-u/report").json()
            self.assertEqual((body["truncated"], body["redacted"]), (True, True))
            self.assertLessEqual(len(body["content"].encode("utf-8")), cap)

            text = ("See /home/alice/private/case.md and `src/app.py`.\n"
                    "Edit dashboard/plugin_api.py; see https://github.com/a/b\n"
                    "(/var/log/x.log) and C:\\Users\\bob\\notes.txt\n")
            report.write_text(text, encoding="utf-8")
            body = client.get(f"{_CP}/workstreams/ws-u/report").json()
            self.assertEqual((body["content"], body["redacted"]),
                             ("[redacted]\nEdit dashboard/plugin_api.py; see https://github.com/a/b\n[redacted]\n", True))
            report.write_text("plain src/app.py https://github.com/a/b\n", encoding="utf-8")
            self.assertFalse(client.get(f"{_CP}/workstreams/ws-u/report").json()["redacted"])

    def test_root_independent_absolute_paths_redacted(self):
        with isolated_hermes_home() as home:
            client = _cp_client("factonio_test_cp_report_roots")
            run = self._runs(home) / "ws-r"
            run.mkdir(parents=True)
            report = run / "investigation.md"
            leaks = (
                "See /data/customer/private.txt now.", "Open /workspace/project/secrets.txt.",
                "[key](/srv2/app/key.pem)", "Home /home/alice/private/case.md", "Config in /etc/",
                "Root /\u5ba2\u6237/private.txt", "Root /a+b/private.txt", "Root /my data/private.txt",
                'Quoted "/my data/private.txt"', 'Quoted "/my data/sub dir/secret.txt"',
                "\uff08/\u5ba2\u6237/private.txt\uff09", "src/app.py https://example.com/a/b /my data/x",
                "Log at C:/Users/alice/private.txt", "Notes C:\\Program Files\\secret.txt", "Log at D:/\u5ba2\u6237/x",
                "Share \\\\fileserver\\share\\secret.txt", "Share //fileserver/share/secret.txt",
                "Link file:///my data/secret.md here", "Link FILE://host/share/x.md here",
            )
            for leak in leaks:
                with self.subTest(text=leak):
                    report.write_text(f"intro src/app.py\n{leak}\noutro\n", encoding="utf-8")
                    body = client.get(f"{_CP}/workstreams/ws-r/report").json()
                    self.assertEqual((body["content"], body["redacted"]), ("intro src/app.py\n[redacted]\noutro\n", True))
            kept = ("Edit src/app.py and dashboard/plugin_api.py; see https://github.com/omnim-ai/factonio/pull/47,\n"
                    "http://localhost:8000/api/plugins/factonio/cards, foo/bar prose, v1/v2 and the /factonio page.\n"
                    "# Heading\n`code` [link](https://example.com/a/b)\n./a/b ../a/b\n"
                    "src/app.py https://example.com/a/b /factonio\n// ordinary comment\n")
            report.write_text(kept, encoding="utf-8")
            body = client.get(f"{_CP}/workstreams/ws-r/report").json()
            self.assertEqual((body["content"], body["redacted"]), (kept, False))

class TestHarnessAuditApi(unittest.TestCase):
    def test_validation_status_codes_and_read_only_audit(self):
        from factonio.config import get_repo
        from factonio.harness import bootstrap_verify_harness
        from tests.test_verify_harness import _commit_all, _index_fingerprint

        with isolated_hermes_home() as home:
            client = _cp_client("factonio_test_cp_harness")
            self.assertEqual(client.post(f"{_CP}/repos/acme-svc/harness/audit").status_code, 404)
            repo = _cp_register(home, git_repo=True)
            bootstrap_verify_harness("acme/svc")
            _commit_all(repo, "harness")
            for payload in ([], "x", {"base_ref": "-x"}, {"base_ref": "--output=/tmp/x"}, {"base_ref": 5},
                            {"base_ref": None}, {"base_ref": "main\n"}, {"base_ref": "a b"}, {"force": True}):
                with self.subTest(payload=payload):
                    self.assertEqual(client.post(f"{_CP}/repos/acme-svc/harness/audit", json=payload).status_code, 422)
            index_before = _index_fingerprint(repo)
            files_before = sorted(p.name for p in repo.rglob("*") if ".git" not in p.parts)
            r = client.post(f"{_CP}/repos/acme-svc/harness/audit")
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertEqual((body["ok"], body["slug"], body["repo"], body["present"]), (True, "acme-svc", "acme/svc", True))
            for key in ("mode", "severity", "drifted", "drift_classification", "stale_invariants", "manifest_errors",
                        "gate_loosening", "recommendations"):
                self.assertIn(key, body)
            self.assertNotIn("repo_path", body)
            self.assertNotIn(str(repo), r.text)
            self.assertEqual(client.post(f"{_CP}/repos/acme-svc/harness/audit", json={"base_ref": "HEAD"}).status_code, 200)
            bad_ref = client.post(f"{_CP}/repos/acme-svc/harness/audit", json={"base_ref": "nope"})
            self.assertEqual(bad_ref.status_code, 422)
            self.assertNotIn(str(repo), bad_ref.text)
            self.assertNotIn(str(repo.resolve()), bad_ref.text)
            self.assertEqual(_index_fingerprint(repo), index_before)
            self.assertEqual(sorted(p.name for p in repo.rglob("*") if ".git" not in p.parts), files_before)
            rows = _trail_rows("harness_audit")
            self.assertEqual(len(rows), 2)
            self.assertEqual({(r["actor"], r["phase"], r["repo_slug"]) for r in rows}, {("human", "harness", "acme-svc")})
            self.assertEqual(rows[0]["result"], body["severity"])

            import shutil

            shutil.rmtree(Path(get_repo("acme-svc")["path"]))
            self.assertEqual(client.post(f"{_CP}/repos/acme-svc/harness/audit").status_code, 409)

    def test_repos_listing_stays_subprocess_free(self):
        import subprocess as sp
        from unittest import mock

        with isolated_hermes_home() as home:
            _cp_register(home, git_repo=True)
            client = _cp_client("factonio_test_cp_repos_ro")
            with mock.patch.object(sp, "run", side_effect=AssertionError("no subprocess")), \
                    mock.patch.object(sp, "Popen", side_effect=AssertionError("no subprocess")):
                self.assertEqual(client.get(f"{_CP}/repos").status_code, 200)


class TestPanelAndArenaEvaluateApi(unittest.TestCase):
    def test_panel_evaluate(self):
        from factonio.orchestrator import spawn_verification_pair
        from tests.test_local_publish_gate import TWO_VENDORS

        with isolated_hermes_home() as home:
            _cp_register(home)
            client = _cp_client("factonio_test_cp_panel")
            url = f"{_CP}/panels/evaluate"
            for payload in (None, [], {}, {"pr_number": 3, "branch": "feat/x"}, {"pr_number": True},
                            {"pr_number": "3"}, {"pr_number": 0}, {"pr_number": 2.0}, {"branch": ""},
                            {"branch": 5}, {"panel_id": None}, {"tenant": "../x"}, {"record": False},
                            {"tenant": "ws", "board": "x"}):
                with self.subTest(payload=payload):
                    self.assertEqual(client.post(url, json=payload).status_code, 422)
            self.assertEqual(client.post(url, json={"pr_number": 9}).status_code, 404)
            res = spawn_verification_pair("acme/svc", "ws-pan", 9, rigor="high", panel_models=TWO_VENDORS)
            for lane in res["panel"]["lanes"]:
                _cp_complete(lane["verify_diff_task_id"], "pass", findings=["see holdout/s.md"])
                _cp_complete(lane["verify_blackbox_task_id"], "pass")
            r = client.post(url, json={"tenant": "ws-pan", "pr_number": 9})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual((r.json()["consensus"], r.json()["merge_allowed"]), ("pass", True))
            self.assertNotIn("holdout", r.text.lower())
            self.assertEqual(len(_trail_rows("verify_panel_consensus")), 1)
            again = client.post(url, json={"panel_id": res["panel"]["panel_id"]})
            self.assertEqual((again.status_code, again.json()["recorded"]), (200, False))
            self.assertEqual(len(_trail_rows("verify_panel_consensus")), 1)

    def test_arena_evaluate_is_read_only_and_path_free(self):
        with isolated_hermes_home() as home:
            client = _cp_client("factonio_test_cp_arena")
            url = f"{_CP}/arenas/evaluate"
            for payload in (None, [], {}, {"arena_id": "ar_1111111111", "spawn_judge": True}, {"arena_id": 5},
                            {"arena_id": "../ar_1"}, {"arena_id": "AR_11111111"}, {"tenant": "../x"},
                            {"tenant": "ws", "judge_model": "x"}):
                with self.subTest(payload=payload):
                    self.assertEqual(client.post(url, json=payload).status_code, 422)
            self.assertEqual(client.post(url, json={"arena_id": "ar_1111111111"}).status_code, 404)
            aid = "ar_1111111111"
            _seed_arena_cards(home / "kanban.db", time.time(), {
                "t_a1": _arena_stamp(aid, "candidate", "define", "anthropic", slot="c1"),
                "t_a2": _arena_stamp(aid, "candidate", "define", "openai", slot="c2"),
            })
            _write_arena_manifest(home, "ws-arena", aid, {
                "arena_id": aid, "tenant": "ws-arena", "repo": "acme/svc", "target": "define",
                "title": "Pick a spec", "created_at_ns": 5,
                "candidates": [
                    {"slot": "c1", "vendor": "anthropic", "model": "holdout/m", "task_id": "t_a1",
                     "dir": str(home / "runs" / "c1"), "spec_path": str(home / "holdout" / "spec.md")},
                    {"slot": "c2", "vendor": "openai", "model": "gpt", "task_id": "t_a2", "dir": str(home / "c2")},
                ],
            }, raw=None)
            data_dir = home / "plugin-data"

            def snapshot() -> list[tuple[str, int]]:
                return sorted((str(p), p.stat().st_mtime_ns) for p in data_dir.rglob("*"))

            before = snapshot()
            for payload in ({"arena_id": aid}, {"tenant": "ws-arena"}, {"arena_id": aid, "tenant": "ws-arena"}):
                with self.subTest(payload=payload):
                    r = client.post(url, json=payload)
                    self.assertEqual(r.status_code, 200, r.text)
                    body = r.json()
                    self.assertNotIn("holdout", r.text.lower())
                    self.assertNotIn(str(home), r.text)
                    self.assertNotIn("manifest_path", body)
                    self.assertEqual((body["ok"], body["arena_id"], body["tenant"], body["repo"]),
                                     (True, aid, "ws-arena", "acme/svc"))
                    self.assertEqual([c["model"] for c in body["candidates"]], [None, "gpt"])
                    self.assertIsNone(body["judge"])
            self.assertEqual(snapshot(), before, "never spawns a judge or writes a manifest")
            from hermes_cli.kanban_db_connect import connect_closing

            with connect_closing(db_path=home / "kanban.db") as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 2)
            self.assertEqual(_trail_rows("spawn_arena_judge"), [])


    def test_arena_evaluate_malformed_candidates_is_404_not_500(self):
        with isolated_hermes_home() as home:
            client = _cp_client("factonio_test_cp_arena_malformed")
            url = f"{_CP}/arenas/evaluate"
            for i, bad in enumerate((123, True, "c1", {"slot": "c1"})):
                aid = f"ar_{i + 2}{i + 2}{i + 2}{i + 2}222222"
                with self.subTest(candidates=bad):
                    _write_arena_manifest(home, "ws-c", aid, {"arena_id": aid, "tenant": "ws-c", "candidates": bad,
                                                               "created_at_ns": 10 + i})
                    r = client.post(url, json={"arena_id": aid, "tenant": "ws-c"})
                    self.assertEqual(r.status_code, 404, r.text)
                    self.assertEqual(client.post(url, json={"tenant": "ws-c"}).status_code, 404)
                    cards = client.get(f"{_CP}/cards")
                    self.assertEqual(cards.status_code, 200, cards.text)
            good = "ar_9999999999"
            _write_arena_manifest(home, "ws-c", good, {"arena_id": good, "tenant": "ws-c", "candidates": [],
                                                        "created_at_ns": 1})
            self.assertEqual(client.post(url, json={"arena_id": good}).status_code, 200)
            self.assertEqual(client.post(url, json={"tenant": "ws-c"}).status_code, 404, "newest manifest is corrupt")

if __name__ == "__main__":
    unittest.main()
