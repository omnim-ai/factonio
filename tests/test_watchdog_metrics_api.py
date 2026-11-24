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


if __name__ == "__main__":
    unittest.main()
