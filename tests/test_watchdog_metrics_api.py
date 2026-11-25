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
                    {"merge_policy": "auto", "max_fix_rounds": 3, "design_provider": "auto", "design_approval": "ask"},
                )
                item = body["repos"][0]
                for key in ("slug", "repo", "url", "path", "default_branch", "merge_policy", "max_fix_rounds",
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


if __name__ == "__main__":
    unittest.main()
