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
                ("hook not object", {"schema_version": 1, "hooks": [1, _HOOKS[0]]}, None),
            ):
                with self.subTest(label=label):
                    _write_harness(repo, manifest, raw=raw)
                    out = summarize_harness_readonly(repo)
                    self.assertTrue(out["has_manifest_error"])
                    self.assertEqual(set(out), set(self._ABSENT))
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
            for spec, path in (("acme/good", good), ("acme/bad", bad), ("acme/plain", plain)):
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


if __name__ == "__main__":
    unittest.main()
