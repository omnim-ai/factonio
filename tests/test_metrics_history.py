"""Clean-room tests for `trail.db` schema v2 long-term metric snapshots: the v1 -> v2 migration,
append-only `metric_snapshots`, best-effort capture and milestone hooks, `GET /metrics/history`,
the additive `/metrics` `history` block, and `hermes factonio metrics`."""

from __future__ import annotations

import contextlib
import gc
import json
import os
import sqlite3
import subprocess
import sys
import time
import unittest
import warnings
from pathlib import Path
from typing import Any
from unittest import mock

from tests._env import PLUGIN_ROOT, isolated_hermes_home

if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

from tests.test_trail import _api_client, _no_worker_env, _tools
from tests.test_verify_panel import TWO_VENDORS, _cli, _complete, _register
from tests.test_watchdog_metrics_api import _seed_kanban_db

_V1_META = "INSERT INTO schema_meta(key, value, updated_at) VALUES ('schema_version', ?, '2026-01-01T00:00:00.000000Z')"


def _make_v1_store(db: Path, decisions: int = 0) -> list[tuple]:
    """Build a store exactly as v1 code left it (v1 DDL + `schema_version = "1"`), with decisions."""
    from factonio import trail

    db.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(str(db))) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(trail._SCHEMA_SQL)
        conn.execute(_V1_META, ("1",))
        prev = None
        for i in range(decisions):
            row_id = f"d_{i:012x}"
            conn.execute(
                "INSERT INTO decisions(id, ts, tenant, repo_slug, phase, card_id, actor, decision, why, evidence_json, "
                "result, supersedes_id) VALUES (?, ?, ?, 'acme-api', 'verify', ?, 'orchestrator', ?, 'why', ?, 'ok', ?)",
                (row_id, f"2026-01-01T00:00:{i % 60:02d}.{i:06d}Z", f"t{i % 3}", f"t_{i}", f"decision_{i}",
                 json.dumps({"n": i}), prev if i % 5 == 4 else None),
            )
            prev = row_id
        conn.commit()
        return _dump_decisions(conn)


def _dump_decisions(conn: sqlite3.Connection) -> list[tuple]:
    return [tuple(r) for r in conn.execute("SELECT * FROM decisions ORDER BY rowid").fetchall()]


def _meta(db: Path) -> dict[str, str]:
    with contextlib.closing(sqlite3.connect(str(db))) as conn:
        return {k: v for k, v, _ in conn.execute("SELECT key, value, updated_at FROM schema_meta").fetchall()}


def _snapshot_metrics(**kpis: Any) -> dict[str, Any]:
    return {"range": "all", **kpis}


_SNAPSHOT_CHILD = """
import sys, time
from pathlib import Path
from factonio import trail
db, go, tag = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
while not go.exists():
    time.sleep(0.001)
try:
    if tag.endswith("s"):
        trail.record_metric_snapshot({"cards_total": 1}, source="mp", tenant=tag, db_path=db)
    else:
        trail.log_decision("explicit:" + tag, tenant="mp", db_path=db)
except Exception as exc:
    print(repr(exc), file=sys.stderr)
    sys.exit(3)
"""


class TestSchemaV2Migration(unittest.TestCase):
    def test_fresh_store_is_v2_with_snapshot_table_indexes_and_triggers(self):
        from factonio import trail

        with isolated_hermes_home():
            trail.record_metric_snapshot(_snapshot_metrics(), source="manual")
            path = trail.trail_db_path()
            self.assertEqual(_meta(path)["schema_version"], "2")
            with contextlib.closing(trail._connect()) as conn:
                cols = [r[1] for r in conn.execute("PRAGMA table_info(metric_snapshots)").fetchall()]
                self.assertEqual(tuple(cols), trail.SNAPSHOT_COLUMNS)
                self.assertEqual(cols[0], "id")
                self.assertEqual(cols[-1], "payload_json")
                triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
                self.assertEqual(triggers, {
                    "trg_decisions_no_update", "trg_decisions_no_delete",
                    "trg_metric_snapshots_no_update", "trg_metric_snapshots_no_delete",
                })
                indexed = set()
                for (idx,) in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='metric_snapshots' AND sql IS NOT NULL"
                ):
                    indexed.add(tuple(r[2] for r in conn.execute(f"PRAGMA index_info({idx})")))
                self.assertEqual(indexed, {("ts",), ("repo_slug", "ts"), ("tenant", "ts"), ("source", "ts")})
                types = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(metric_snapshots)").fetchall()}
                self.assertEqual(types["cards_total"], "INTEGER")
                self.assertEqual(types["diff_pass_rate"], "REAL")
                self.assertEqual(types["median_verify_blackbox_s"], "REAL")

    def test_v1_store_migrates_preserving_every_decision_idempotently(self):
        from factonio import trail

        with isolated_hermes_home() as home:
            db = home / "legacy" / "trail.db"
            before = _make_v1_store(db, decisions=40)
            self.assertEqual(_meta(db)["schema_version"], "1")

            rows = trail.query_decisions(db_path=db, limit=1000)
            self.assertEqual(len(rows), 40)
            self.assertEqual(_meta(db)["schema_version"], "2")
            with contextlib.closing(sqlite3.connect(str(db))) as conn:
                self.assertEqual(_dump_decisions(conn), before)
                stamp = conn.execute("SELECT updated_at FROM schema_meta WHERE key='schema_version'").fetchone()[0]
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM metric_snapshots").fetchone()[0], 0)
            superseded = [r for r in rows if r["is_superseded"]]
            self.assertEqual(len(superseded), 8)

            trail.record_metric_snapshot(_snapshot_metrics(cards_total=3), db_path=db)
            trail.log_decision("after_migration", db_path=db)
            with contextlib.closing(sqlite3.connect(str(db))) as conn:
                self.assertEqual(_dump_decisions(conn)[:40], before)
                self.assertEqual(
                    conn.execute("SELECT updated_at FROM schema_meta WHERE key='schema_version'").fetchone()[0], stamp
                )
            self.assertEqual(trail.summarize_trail(db_path=db)["total"], 41)
            self.assertEqual(len(trail.query_metric_snapshots(db_path=db)), 1)

    def test_missing_version_migrates_and_newer_version_is_never_downgraded(self):
        from factonio import trail

        with isolated_hermes_home() as home:
            unversioned = home / "unversioned" / "trail.db"
            unversioned.parent.mkdir(parents=True)
            with contextlib.closing(sqlite3.connect(str(unversioned))) as conn:
                conn.executescript(trail._SCHEMA_SQL)
            trail.record_metric_snapshot(_snapshot_metrics(), db_path=unversioned)
            self.assertEqual(_meta(unversioned)["schema_version"], "2")

            newer = home / "newer" / "trail.db"
            newer.parent.mkdir(parents=True)
            with contextlib.closing(sqlite3.connect(str(newer))) as conn:
                conn.executescript(trail._SCHEMA_SQL)
                conn.executescript(trail._MIGRATION_V2_SQL)
                conn.execute(_V1_META, ("3",))
                conn.commit()
            trail.log_decision("on_newer", db_path=newer)
            trail.record_metric_snapshot(_snapshot_metrics(), db_path=newer)
            self.assertEqual(_meta(newer)["schema_version"], "3")

    def test_concurrent_cold_v1_migration_with_snapshot_writes_across_processes(self):
        from factonio import trail

        with isolated_hermes_home() as home:
            env = dict(os.environ)
            env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
            go = home / "go"
            stores = [home / f"mig-{i}" / "trail.db" for i in range(4)]
            baselines = {db: _make_v1_store(db, decisions=10) for db in stores}
            procs = []
            for db in stores:
                for n in range(8):
                    tag = f"{db.parent.name}-{n}{'s' if n % 2 else 'd'}"
                    procs.append(subprocess.Popen(
                        [sys.executable, "-c", _SNAPSHOT_CHILD, str(db), str(go), tag],
                        env=env, stderr=subprocess.PIPE, text=True,
                    ))
            time.sleep(1.0)
            go.touch()
            results = [(p.wait(timeout=120), p.stderr.read()) for p in procs]
            for p in procs:
                p.stderr.close()
            failed = [r for r in results if r[0] != 0]
            self.assertEqual(failed, [], f"{len(failed)}/{len(procs)} concurrent migrate+write children failed")
            for db in stores:
                self.assertEqual(_meta(db)["schema_version"], "2")
                self.assertEqual(len(trail.query_metric_snapshots(db_path=db, limit=1000)), 4, db)
                self.assertEqual(trail.summarize_trail(db_path=db)["total"], 14, db)
                with contextlib.closing(sqlite3.connect(str(db))) as conn:
                    self.assertEqual(_dump_decisions(conn)[:10], baselines[db])

    def test_concurrent_cold_v1_migration_threads(self):
        import threading

        from factonio import trail

        with isolated_hermes_home() as home:
            stores = [home / f"th-{i}" / "trail.db" for i in range(6)]
            for db in stores:
                _make_v1_store(db, decisions=3)
            errors: list[str] = []
            barrier = threading.Barrier(len(stores) * 6)

            def writer(db: Path, n: int) -> None:
                barrier.wait()
                try:
                    if n % 2:
                        trail.record_metric_snapshot({"cards_total": n}, source="thread", db_path=db)
                    else:
                        trail.log_decision(f"t{n}", db_path=db)
                except Exception as exc:  # collected and asserted below
                    errors.append(repr(exc))

            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", ResourceWarning)
                threads = [threading.Thread(target=writer, args=(db, n)) for db in stores for n in range(6)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=120)
                gc.collect()
            self.assertEqual(errors, [])
            for db in stores:
                self.assertEqual(len(trail.query_metric_snapshots(db_path=db)), 3)
                self.assertEqual(trail.summarize_trail(db_path=db)["total"], 6)
            leaked = [w for w in caught if issubclass(w.category, ResourceWarning)]
            self.assertEqual(leaked, [], [str(w.message) for w in leaked])


class TestSnapshotStore(unittest.TestCase):
    def test_snapshots_are_append_only(self):
        from factonio import trail

        with isolated_hermes_home():
            snap = trail.record_metric_snapshot(_snapshot_metrics(cards_total=2))
            self.assertRegex(snap["id"], r"^ms_[0-9a-f]{12}$")
            with contextlib.closing(sqlite3.connect(str(trail.trail_db_path()), isolation_level=None)) as raw:
                with self.assertRaisesRegex(sqlite3.DatabaseError, "append-only"):
                    raw.execute("UPDATE metric_snapshots SET cards_total = 99 WHERE id = ?", (snap["id"],))
                with self.assertRaisesRegex(sqlite3.DatabaseError, "append-only"):
                    raw.execute("DELETE FROM metric_snapshots WHERE id = ?", (snap["id"],))
                with self.assertRaisesRegex(sqlite3.DatabaseError, "append-only"):
                    raw.execute("DELETE FROM metric_snapshots")
            rows = trail.query_metric_snapshots()
            self.assertEqual([(r["id"], r["cards_total"]) for r in rows], [(snap["id"], 2)])

    def test_record_flattens_metrics_validates_and_is_idempotent_on_retry(self):
        from factonio import trail

        metrics = {
            "range": "30d",
            "totals": {"cards": 9, "done": 5, "running": 2, "blocked": 1, "triage": 1, "workstreams": 3},
            "phases": {"define": {"median_seconds": 120.0}, "verify-blackbox": {"median_seconds": 80.5}},
            "verification": {"diff": {"pass": 3, "reject": 1, "inconclusive": 0, "pass_rate": 0.75},
                             "blackbox": {"pass": 2, "reject": 0, "inconclusive": 1, "pass_rate": 0.667}},
            "rework": {"avg_runs_per_implement_card": 1.5, "cards_with_rework": 1, "first_pass_rate": 0.5},
            "escapes": {"count": 1, "verified_total": 3, "escape_rate": 0.333},
            "verification_panels": {"total": 2, "pass": 1, "reject": 1, "inconclusive": 0, "pending": 0,
                                    "disagreements": 1},
            "trail": {"decision_count": 17},
        }
        with isolated_hermes_home() as home:
            _register(home, "acme/api")
            real_connect = trail._connect
            calls = {"n": 0}

            class _FlakyConn:
                """Commits for real, then reports a transient lock (simulating a lost ack)."""

                def __init__(self, conn: sqlite3.Connection) -> None:
                    self._conn = conn

                def __getattr__(self, name: str) -> Any:
                    return getattr(self._conn, name)

                def commit(self) -> None:
                    self._conn.commit()
                    if calls["n"] == 1:
                        raise sqlite3.OperationalError("database is locked")

            def flaky_connect(db_path=None, **kwargs):
                calls["n"] += 1
                return _FlakyConn(real_connect(db_path, **kwargs))

            with mock.patch.object(trail, "_connect", side_effect=flaky_connect):
                snap = trail.record_metric_snapshot(
                    metrics, source="merge", repo_slug="acme/api", tenant="acme-api-i1", extra={"pr_number": 9}
                )
            self.assertEqual(calls["n"], 2)
            self.assertEqual(len(trail.query_metric_snapshots()), 1)
            expected = {
                "source": "merge", "range_key": "30d", "repo_slug": "acme-api", "tenant": "acme-api-i1",
                "workstreams_total": 3, "cards_total": 9, "cards_done": 5, "cards_active": 2, "cards_blocked": 1,
                "diff_pass": 3, "diff_reject": 1, "diff_inconclusive": 0, "diff_pass_rate": 0.75,
                "blackbox_pass": 2, "blackbox_reject": 0, "blackbox_inconclusive": 1, "blackbox_pass_rate": 0.667,
                "first_pass_rate": 0.5, "escape_count": 1, "verified_total": 3, "escape_rate": 0.333,
                "rework_cards": 1, "avg_runs_per_implement_card": 1.5, "median_define_s": 120.0,
                "median_design_s": None, "median_implement_s": None, "median_verify_diff_s": None,
                "median_verify_blackbox_s": 80.5, "panels_total": 2, "panels_pass": 1, "panels_reject": 1,
                "panels_inconclusive": 0, "panels_pending": 0, "panels_disagreements": 1,
                "trail_decisions_total": 17,
            }
            self.assertEqual({k: snap[k] for k in expected}, expected)
            self.assertEqual(list(snap)[: len(trail.SNAPSHOT_COLUMNS) - 1], list(trail.SNAPSHOT_COLUMNS[:-1]))
            self.assertEqual(snap["payload"], {"metrics": metrics, "extra": {"pr_number": 9}})

            with self.assertRaisesRegex(ValueError, "mapping"):
                trail.record_metric_snapshot(["nope"])  # type: ignore[arg-type]
            with self.assertRaisesRegex(ValueError, "JSON object"):
                trail.record_metric_snapshot({}, extra=["nope"])  # type: ignore[arg-type]
            with self.assertRaises(ValueError):
                trail.record_metric_snapshot({}, ts="yesterday-ish")

    def test_query_filters_since_limit_and_chronological_order(self):
        from factonio import trail

        with isolated_hermes_home() as home:
            self.assertEqual(trail.query_metric_snapshots(), [])
            self.assertEqual(trail.summarize_metric_snapshots()["total"], 0)
            self.assertFalse(trail.trail_db_path().exists(), "queries must never create trail.db")

            _register(home, "acme/api")
            now = time.time()
            seeded = []
            for i, (age_days, source, repo, tenant) in enumerate([
                (120, "merge", "acme/api", "t1"),
                (45, "merge", "acme/api", "t1"),
                (20, "panel_consensus", "acme/web", "t2"),
                (5, "retro", "", ""),
                (1, "merge", "acme/web", "t2"),
                (0, "manual", "acme/api", "t1"),
            ]):
                ts = time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime(now - age_days * 86400 - 60))
                seeded.append(trail.record_metric_snapshot(
                    {"cards_total": i}, source=source, repo_slug=repo or None, tenant=tenant, ts=ts))

            rows = trail.query_metric_snapshots()
            self.assertEqual([r["cards_total"] for r in rows], [0, 1, 2, 3, 4, 5])
            self.assertEqual([r["ts"] for r in rows], sorted(r["ts"] for r in rows))
            self.assertEqual([r["cards_total"] for r in trail.query_metric_snapshots(limit=2)], [4, 5])
            self.assertEqual([r["cards_total"] for r in trail.query_metric_snapshots(limit=2, order="desc")], [5, 4])
            self.assertEqual([r["cards_total"] for r in trail.query_metric_snapshots(since="7d")], [3, 4, 5])
            self.assertEqual([r["cards_total"] for r in trail.query_metric_snapshots(since="30d")], [2, 3, 4, 5])
            self.assertEqual([r["cards_total"] for r in trail.query_metric_snapshots(since="90d")], [1, 2, 3, 4, 5])
            self.assertEqual(len(trail.query_metric_snapshots(since="all")), 6)
            self.assertEqual(
                [r["cards_total"] for r in trail.query_metric_snapshots(since=seeded[2]["ts"])], [2, 3, 4, 5]
            )
            naive = seeded[4]["ts"].rstrip("Z")
            self.assertEqual([r["cards_total"] for r in trail.query_metric_snapshots(since=naive)], [4, 5])
            self.assertEqual([r["cards_total"] for r in trail.query_metric_snapshots(repo="acme/api")], [0, 1, 5])
            self.assertEqual([r["cards_total"] for r in trail.query_metric_snapshots(repo="acme-api")], [0, 1, 5])
            self.assertEqual([r["cards_total"] for r in trail.query_metric_snapshots(tenant="t2")], [2, 4])
            self.assertEqual([r["cards_total"] for r in trail.query_metric_snapshots(source="merge")], [0, 1, 4])
            self.assertEqual(
                [r["cards_total"] for r in trail.query_metric_snapshots(source="merge", tenant="t1", since="90d")], [1]
            )
            self.assertNotIn("payload", trail.query_metric_snapshots(include_payload=False)[0])
            with self.assertRaisesRegex(ValueError, "Invalid since"):
                trail.query_metric_snapshots(since="last tuesday")
            with self.assertRaises(ValueError):
                trail.query_metric_snapshots(order="sideways")

            summary = trail.summarize_metric_snapshots(since="30d")
            self.assertEqual(summary["total"], 4)
            self.assertEqual(summary["by_source"], {"manual": 1, "merge": 1, "panel_consensus": 1, "retro": 1})
            self.assertEqual(summary["by_repo"], {"acme-api": 1, "acme/web": 2, "unscoped": 1})
            self.assertEqual(summary["first_ts"], seeded[2]["ts"])
            self.assertEqual(summary["latest_ts"], seeded[5]["ts"])
            self.assertEqual(summary["latest"]["id"], seeded[5]["id"])
            self.assertNotIn("payload", summary["latest"])

            payload = trail.metrics_history_payload(repo="acme/api", limit=5000)
            self.assertEqual(set(payload), {"snapshots", "summary", "filters"})
            self.assertEqual(payload["filters"]["limit"], 1000)
            self.assertEqual(payload["filters"]["repo"], "acme/api")
            self.assertEqual(payload["summary"]["total"], 3)
            self.assertEqual(len(payload["snapshots"]), 3)
            self.assertEqual(trail.metrics_history_payload(limit=0)["filters"]["limit"], 1)


class TestCaptureAndMetrics(unittest.TestCase):
    def test_compute_metrics_adds_first_pass_rate_and_history_block(self):
        from factonio.metrics import compute_metrics

        with isolated_hermes_home() as home:
            empty = compute_metrics(home / "kanban.db")
            self.assertIsNone(empty["rework"]["first_pass_rate"])
            self.assertEqual(empty["history"], {"snapshot_count": 0, "latest_ts": None})

            _seed_kanban_db(home / "kanban.db", time.time())
            m = compute_metrics(home / "kanban.db", range_key="30d")
            for key in ("range", "tenant", "totals", "phases", "verification", "rework", "escapes",
                        "verification_panels", "trail", "history"):
                self.assertIn(key, m)
            self.assertEqual(m["rework"]["cards_with_rework"], 1)
            self.assertEqual(m["rework"]["avg_runs_per_implement_card"], 1.5)
            self.assertEqual(m["rework"]["first_pass_rate"], 0.5)

    def test_capture_snapshot_survives_kanban_db_reset(self):
        from factonio import trail
        from factonio.metrics import capture_metrics_snapshot, compute_metrics

        with isolated_hermes_home() as home:
            kanban = home / "kanban.db"
            _seed_kanban_db(kanban, time.time())
            snap = capture_metrics_snapshot(kanban, source="manual", tenant="acme-api-i1", repo_slug="acme/api")
            self.assertIsNotNone(snap)
            self.assertEqual(
                (snap["workstreams_total"], snap["cards_total"], snap["diff_pass"], snap["blackbox_pass"]), (3, 6, 1, 1)
            )
            self.assertEqual((snap["rework_cards"], snap["first_pass_rate"], snap["escape_count"]), (1, 0.5, 1))
            self.assertEqual(snap["median_define_s"], 120.0)
            self.assertEqual((snap["tenant"], snap["range_key"]), ("acme-api-i1", "all"))
            self.assertEqual(snap["payload"]["metrics"]["totals"]["cards"], 6)

            scoped = capture_metrics_snapshot(kanban, tenant="acme-api-i2", scope_to_tenant=True)
            self.assertEqual((scoped["workstreams_total"], scoped["cards_total"]), (1, 1))

            for suffix in ("", "-wal", "-shm"):
                Path(str(kanban) + suffix).unlink(missing_ok=True)
            after_reset = compute_metrics(kanban, range_key="all")
            self.assertEqual(after_reset["totals"]["cards"], 0)
            self.assertEqual(after_reset["history"]["snapshot_count"], 2)
            self.assertEqual(after_reset["history"]["latest_ts"], scoped["ts"])
            self.assertEqual(compute_metrics(kanban, tenant="acme-api-i1")["history"]["snapshot_count"], 1)

            fresh = capture_metrics_snapshot(kanban, source="manual")
            history = trail.query_metric_snapshots()
            self.assertEqual([r["id"] for r in history], [snap["id"], scoped["id"], fresh["id"]])
            self.assertEqual([r["cards_total"] for r in history], [6, 1, 0])

    def test_capture_is_best_effort_on_corrupt_or_unwritable_trail_db(self):
        from factonio import trail
        from factonio.metrics import capture_metrics_snapshot, compute_metrics

        with isolated_hermes_home() as home:
            path = trail.trail_db_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"this is not a sqlite database" * 64)
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", ResourceWarning)
                started = time.monotonic()
                with self.assertLogs("factonio.metrics", level="WARNING") as logs:
                    self.assertIsNone(capture_metrics_snapshot(home / "kanban.db", source="merge"))
                elapsed = time.monotonic() - started
                metrics = compute_metrics(home / "kanban.db")
                gc.collect()
            self.assertLess(elapsed, 2.0)
            self.assertIn("merge", "\n".join(logs.output))
            self.assertEqual(metrics["history"], {"snapshot_count": 0, "latest_ts": None})
            self.assertIn("totals", metrics)
            leaked = [w for w in caught if issubclass(w.category, ResourceWarning)]
            self.assertEqual(leaked, [], [str(w.message) for w in leaked])

            client = _api_client("factonio_test_history_corrupt")
            self.assertEqual(client.get("/api/plugins/factonio/metrics").json()["history"]["snapshot_count"], 0)
            self.assertEqual(client.get("/api/plugins/factonio/metrics/history").status_code, 503)

            with mock.patch("factonio.trail._connect", side_effect=PermissionError("read-only file system")):
                with self.assertLogs("factonio.metrics", level="WARNING"):
                    self.assertIsNone(capture_metrics_snapshot(home / "kanban.db"))

        if hasattr(os, "geteuid") and os.geteuid() == 0:
            return
        with isolated_hermes_home() as home:
            locked = home / "ro"
            locked.mkdir()
            locked.chmod(0o500)
            try:
                with self.assertLogs("factonio.metrics", level="WARNING"):
                    self.assertIsNone(capture_metrics_snapshot(home / "kanban.db", trail_db=locked / "sub" / "trail.db"))
            finally:
                locked.chmod(0o700)


class TestMilestoneHooks(unittest.TestCase):
    def test_confirmed_merge_captures_snapshot_and_queued_merge_does_not(self):
        from factonio import trail
        from tests.test_pr_gate_and_git import HEAD_SHA, FakeGh, _pr

        with isolated_hermes_home() as home, _no_worker_env():
            _register(home, "acme/sample")
            _seed_kanban_db(home / "kanban.db", time.time())
            tools = _tools()
            queued = FakeGh(_pr(), readback={"state": "OPEN", "mergedAt": None, "baseRefName": "main"})
            with mock.patch("factonio.repos._run_gh", queued):
                out = json.loads(tools["factonio_merge_pr"]["handler"](
                    {"repo": "acme/sample", "pr_number": 12, "expected_head_sha": HEAD_SHA, "tenant": "acme-sample-i3"}))
            self.assertTrue(out["queued"])
            self.assertEqual(trail.query_metric_snapshots(), [])

            merged = FakeGh(_pr(), readback={"state": "MERGED", "mergedAt": "2026-11-25T12:00:00Z", "baseRefName": "main"})
            with mock.patch("factonio.repos._run_gh", merged):
                out = json.loads(tools["factonio_merge_pr"]["handler"](
                    {"repo": "acme/sample", "pr_number": 12, "expected_head_sha": HEAD_SHA, "tenant": "acme-sample-i3"}))
            self.assertTrue(out["merged"])
            snaps = trail.query_metric_snapshots()
            self.assertEqual(len(snaps), 1)
            snap = snaps[0]
            self.assertEqual(out["metrics_snapshot_id"], snap["id"])
            self.assertEqual((snap["source"], snap["repo_slug"], snap["tenant"]), ("merge", "acme-sample", "acme-sample-i3"))
            self.assertEqual(snap["payload"]["extra"]["pr_number"], 12)
            self.assertEqual(snap["payload"]["extra"]["head_sha"], HEAD_SHA)
            # Factory-wide KPIs, not scoped to the triggering tenant.
            self.assertEqual(snap["workstreams_total"], 3)
            merge_rows = [r for r in trail.query_decisions(tenant="acme-sample-i3") if r["decision"] == "merge_pr"]
            self.assertEqual([r["result"] for r in merge_rows], ["merged", "queued"])

            with mock.patch("factonio.trail._connect", side_effect=sqlite3.DatabaseError("file is not a database")):
                with mock.patch("factonio.repos._run_gh", merged), self.assertLogs("factonio.metrics", level="WARNING"):
                    from factonio.repos import merge_pr

                    res = merge_pr("acme/sample", 12, expected_head_sha=HEAD_SHA, tenant="acme-sample-i3")
            self.assertTrue(res["merged"])
            self.assertIsNone(res["metrics_snapshot_id"])

    def test_newly_recorded_panel_consensus_captures_one_snapshot(self):
        from factonio import trail
        from factonio.orchestrator import evaluate_verification_panel, spawn_verification_pair

        with isolated_hermes_home() as home:
            _register(home)
            res = spawn_verification_pair("acme/svc", "t-snap", 9, rigor="high", panel_models=TWO_VENDORS)
            pid = res["panel"]["panel_id"]
            pending = evaluate_verification_panel(panel_id=pid)
            self.assertEqual(pending["consensus"], "pending")
            self.assertIsNone(pending["metrics_snapshot_id"])
            self.assertEqual(trail.query_metric_snapshots(), [])

            for lane in res["panel"]["lanes"]:
                _complete(lane["verify_diff_task_id"], "pass")
                _complete(lane["verify_blackbox_task_id"], "pass")
            unrecorded = evaluate_verification_panel(panel_id=pid, record_trail=False)
            self.assertEqual((unrecorded["consensus"], unrecorded["metrics_snapshot_id"]), ("pass", None))
            self.assertEqual(trail.query_metric_snapshots(), [])

            ev = evaluate_verification_panel(panel_id=pid)
            self.assertTrue(ev["recorded"])
            snaps = trail.query_metric_snapshots(source="panel_consensus")
            self.assertEqual(len(snaps), 1)
            self.assertEqual(ev["metrics_snapshot_id"], snaps[0]["id"])
            self.assertEqual((snaps[0]["tenant"], snaps[0]["repo_slug"]), ("t-snap", "acme-svc"))
            self.assertEqual(snaps[0]["payload"]["extra"]["panel_id"], pid)
            self.assertEqual(snaps[0]["payload"]["extra"]["consensus"], "pass")
            self.assertEqual(snaps[0]["payload"]["extra"]["pr_number"], 9)
            self.assertFalse(snaps[0]["payload"]["extra"]["has_disagreement"])
            self.assertEqual((snaps[0]["panels_total"], snaps[0]["panels_pass"]), (1, 1))

            again = evaluate_verification_panel(panel_id=pid)
            self.assertFalse(again["recorded"])
            self.assertIsNone(again["metrics_snapshot_id"])
            self.assertEqual(len(trail.query_metric_snapshots()), 1)

    def test_retro_decision_captures_snapshot_other_phases_do_not(self):
        from factonio import trail

        with isolated_hermes_home() as home, _no_worker_env():
            _register(home, "acme/api")
            tools = _tools()
            handler = tools["factonio_log_decision"]["handler"]
            plain = json.loads(handler({"decision": "pivot", "phase": "implement", "tenant": "acme-api-i9"}))
            self.assertTrue(plain["ok"])
            self.assertNotIn("metrics_snapshot_id", plain)
            self.assertEqual(trail.query_metric_snapshots(), [])

            retro = json.loads(handler({
                "decision": "adopt_lint_rule", "phase": "retro", "repo": "acme/api", "tenant": "acme-api-i9",
                "evidence": {"occurrences": 2},
            }))
            self.assertTrue(retro["ok"])
            snaps = trail.query_metric_snapshots()
            self.assertEqual(len(snaps), 1)
            self.assertEqual(retro["metrics_snapshot_id"], snaps[0]["id"])
            self.assertEqual(
                (snaps[0]["source"], snaps[0]["repo_slug"], snaps[0]["tenant"]), ("retro", "acme-api", "acme-api-i9")
            )
            self.assertEqual(snaps[0]["payload"]["extra"],
                             {"decision_id": retro["decision"]["id"], "decision": "adopt_lint_rule"})
            self.assertEqual(snaps[0]["trail_decisions_total"], 2)

            with mock.patch("factonio.metrics.record_metric_snapshot", side_effect=sqlite3.OperationalError("disk I/O error")):
                with self.assertLogs("factonio.metrics", level="WARNING"):
                    failed = json.loads(handler({"decision": "retro_2", "phase": "retro"}))
            self.assertTrue(failed["ok"])
            self.assertIsNone(failed["metrics_snapshot_id"])

    def test_retro_prompt_mentions_history_and_snapshot_capture(self):
        import factonio

        self.assertIn("hermes factonio metrics --history --json", factonio.RETRO_BLUEPRINT_PROMPT)
        self.assertIn("metric_snapshots", factonio.RETRO_BLUEPRINT_PROMPT)
        self.assertIn("captures a metrics snapshot", factonio.RETRO_BLUEPRINT_PROMPT)
        self.assertTrue(100 < len(factonio.ORCHESTRATOR_PROMPT_SECTION) < 3000)


class TestHistoryApiAndCli(unittest.TestCase):
    def _seed(self) -> list[dict[str, Any]]:
        from factonio import trail

        now = time.time()
        rows = []
        for i, (age_days, source, repo, tenant) in enumerate([
            (60, "merge", "acme/api", "t1"), (10, "panel_consensus", "acme/api", "t1"),
            (3, "merge", "acme/web", "t2"), (1, "retro", "acme/api", "t1"),
        ]):
            ts = time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime(now - age_days * 86400))
            rows.append(trail.record_metric_snapshot(
                {"cards_total": i, "diff_pass_rate": 0.5 + i / 10}, source=source, repo_slug=repo, tenant=tenant, ts=ts))
        return rows

    def test_history_api_filters_order_limits_and_metrics_history_block(self):
        with isolated_hermes_home() as home:
            _register(home, "acme/api")
            client = _api_client("factonio_test_history_api")
            empty = client.get("/api/plugins/factonio/metrics/history")
            self.assertEqual(empty.status_code, 200)
            self.assertEqual(empty.json()["snapshots"], [])
            self.assertEqual(empty.json()["summary"]["total"], 0)
            self.assertEqual(empty.json()["filters"]["limit"], 100)

            _seed_kanban_db(home / "kanban.db", time.time())
            seeded = self._seed()
            base = "/api/plugins/factonio/metrics/history"
            data = client.get(base).json()
            self.assertEqual(set(data), {"snapshots", "summary", "filters"})
            self.assertEqual([s["id"] for s in data["snapshots"]], [r["id"] for r in seeded])
            self.assertEqual([s["diff_pass_rate"] for s in data["snapshots"]], [0.5, 0.6, 0.7, 0.8])
            self.assertEqual(data["summary"]["total"], 4)
            self.assertEqual(data["filters"],
                             {"tenant": None, "repo": None, "source": None, "since": None, "since_ts": None, "limit": 100})

            limited = client.get(base, params={"limit": 2}).json()
            self.assertEqual([s["id"] for s in limited["snapshots"]], [seeded[2]["id"], seeded[3]["id"]])
            self.assertEqual(limited["summary"]["total"], 4)
            self.assertEqual(
                [s["id"] for s in client.get(base, params={"repo": "acme/api"}).json()["snapshots"]],
                [seeded[0]["id"], seeded[1]["id"], seeded[3]["id"]],
            )
            self.assertEqual(
                [s["id"] for s in client.get(base, params={"tenant": "t2"}).json()["snapshots"]], [seeded[2]["id"]]
            )
            self.assertEqual(
                [s["id"] for s in client.get(base, params={"source": "merge", "since": "30d"}).json()["snapshots"]],
                [seeded[2]["id"]],
            )
            iso = client.get(base, params={"since": seeded[1]["ts"]}).json()
            self.assertEqual(len(iso["snapshots"]), 3)
            self.assertEqual(iso["filters"]["since_ts"], seeded[1]["ts"])
            self.assertEqual(len(client.get(base, params={"since": "all"}).json()["snapshots"]), 4)
            for bad in ({"limit": 0}, {"limit": 1001}, {"since": "soon"}):
                self.assertEqual(client.get(base, params=bad).status_code, 422, bad)
            self.assertEqual(client.get(base, params={"limit": 1000}).status_code, 200)

            m = client.get("/api/plugins/factonio/metrics?range=30d").json()
            for key in ("range", "tenant", "totals", "phases", "verification", "rework", "escapes",
                        "verification_panels", "trail"):
                self.assertIn(key, m)
            self.assertEqual(m["totals"]["workstreams"], 3)
            self.assertEqual(m["rework"]["cards_with_rework"], 1)
            self.assertEqual(m["rework"]["first_pass_rate"], 0.5)
            self.assertEqual(m["history"], {"snapshot_count": 4, "latest_ts": seeded[3]["ts"]})
            self.assertEqual(client.get("/api/plugins/factonio/metrics?tenant=t2").json()["history"]["snapshot_count"], 1)
            self.assertEqual(client.get("/api/plugins/factonio/trail").status_code, 200)

    def test_cli_metrics_json_snapshot_and_history(self):
        from factonio import trail

        with isolated_hermes_home() as home, _no_worker_env():
            _register(home, "acme/api")
            _seed_kanban_db(home / "kanban.db", time.time())
            run = _cli()

            code, out, _ = run("metrics", "--json")
            self.assertEqual(code, 0)
            data = json.loads(out)
            self.assertEqual(data["range"], "30d")
            self.assertEqual(data["totals"]["workstreams"], 3)
            self.assertEqual(data["history"], {"snapshot_count": 0, "latest_ts": None})

            code, out, _ = run("metrics")
            self.assertEqual(code, 0)
            self.assertIn("Factonio metrics (range=30d)", out)
            self.assertIn("first_pass_rate=50.0%", out)
            self.assertIn("History: 0 snapshot(s)", out)

            code, out, _ = run("metrics", "--snapshot", "--repo", "acme/api", "--tenant", "acme-api-i1", "--json")
            self.assertEqual(code, 0)
            snap = json.loads(out)
            self.assertTrue(snap["ok"])
            self.assertEqual(
                (snap["snapshot"]["source"], snap["snapshot"]["repo_slug"], snap["snapshot"]["tenant"]),
                ("manual", "acme-api", "acme-api-i1"),
            )
            self.assertEqual(snap["snapshot"]["range_key"], "all")
            self.assertEqual(snap["snapshot"]["workstreams_total"], 3)

            code, out, _ = run("--json", "metrics", "--snapshot", "--range", "7d")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["snapshot"]["range_key"], "7d")

            code, out, _ = run("metrics", "--snapshot")
            self.assertEqual(code, 0)
            self.assertIn("Captured metric snapshot ms_", out)

            code, out, _ = run("metrics", "--history", "--json", "--limit", "2")
            self.assertEqual(code, 0)
            hist = json.loads(out)
            self.assertEqual(hist["summary"]["total"], 3)
            self.assertEqual(len(hist["snapshots"]), 2)
            ts = [s["ts"] for s in hist["snapshots"]]
            self.assertEqual(ts, sorted(ts))

            code, out, _ = run("metrics", "--history", "--repo", "acme/api", "--json")
            self.assertEqual([s["id"] for s in json.loads(out)["snapshots"]], [snap["snapshot"]["id"]])

            code, out, _ = run("metrics", "--history", "--snapshot", "--tenant", "acme-api-i1", "--json")
            self.assertEqual(code, 0)
            both = json.loads(out)
            self.assertEqual(both["snapshot"]["id"], both["snapshots"][-1]["id"])
            self.assertEqual(both["summary"]["total"], 2)

            code, out, _ = run("metrics", "--history", "--since", "7d")
            self.assertEqual(code, 0)
            self.assertIn("Metric snapshots: total=4", out)
            self.assertIn("[manual]", out)
            self.assertEqual(out.count("ms_"), 4)

            code, out, err = run("metrics", "--history", "--since", "whenever")
            self.assertEqual(code, 1)
            self.assertIn("Invalid since", err)
            code, out, _ = run("metrics", "--history", "--limit", "0", "--json")
            self.assertEqual(code, 1)
            self.assertFalse(json.loads(out)["ok"])

            self.assertEqual(len(trail.query_metric_snapshots()), 4)

            path = trail.trail_db_path()
            for suffix in ("", "-wal", "-shm"):
                Path(str(path) + suffix).unlink(missing_ok=True)
            path.write_bytes(b"this is not a sqlite database" * 64)
            code, out, _ = run("metrics", "--snapshot", "--json")
            self.assertEqual(code, 1)
            self.assertIn("could not capture", json.loads(out)["error"])
            code, out, _ = run("metrics", "--json")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["history"]["snapshot_count"], 0)


_FLOCK_HOLDER = """
import fcntl, sys
fh = open(sys.argv[1], "a")
fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
print("locked", flush=True)
sys.stdin.read()
"""


def _make_rollback_v1_store(db: Path) -> None:
    """A pre-WAL v1 store (rollback journal), where an EXCLUSIVE transaction also blocks readers."""
    from factonio import trail

    db.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(str(db))) as conn:
        conn.executescript(trail._SCHEMA_SQL)
        conn.execute(_V1_META, ("1",))
        conn.commit()


@contextlib.contextmanager
def _exclusive_txn(db: Path):
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        conn.execute("BEGIN EXCLUSIVE")
        yield conn
    finally:
        conn.rollback()
        conn.close()


@contextlib.contextmanager
def _flock_held_by_child(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([sys.executable, "-c", _FLOCK_HOLDER, str(lock_path)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "locked"
        yield proc
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)
        proc.stdout.close()


@unittest.skipIf(sys.platform == "win32", "POSIX flock")
class TestBestEffortLockBudget(unittest.TestCase):
    def test_held_trail_lock_never_stalls_capture_or_confirmed_merge(self):
        from factonio import trail
        from factonio.metrics import capture_metrics_snapshot
        from factonio.repos import merge_pr
        from tests.test_pr_gate_and_git import HEAD_SHA, FakeGh, _pr

        with isolated_hermes_home() as home, _no_worker_env():
            _register(home, "acme/sample")
            _seed_kanban_db(home / "kanban.db", time.time())
            db = trail.trail_db_path()
            self.assertFalse(db.exists())
            merged = FakeGh(_pr(), readback={"state": "MERGED", "mergedAt": "2026-11-25T12:00:00Z", "baseRefName": "main"})
            with _flock_held_by_child(db.with_name(db.name + ".lock")):
                start = time.monotonic()
                with self.assertLogs("factonio.metrics", level="WARNING"):
                    snap = capture_metrics_snapshot(source="merge", repo_slug="acme-sample", tenant="t1")
                self.assertLess(time.monotonic() - start, 2.0)
                self.assertIsNone(snap)

                start = time.monotonic()
                with mock.patch("factonio.repos._run_gh", merged), self.assertLogs("factonio.metrics", level="WARNING"):
                    res = merge_pr("acme/sample", 12, expected_head_sha=HEAD_SHA, tenant="t1")
                self.assertLess(time.monotonic() - start, 2.0)
                self.assertTrue(res["merged"])
                self.assertIsNone(res["metrics_snapshot_id"])

                # Durable writes still wait for the lock, but only up to a bounded deadline.
                with mock.patch.object(trail, "_DURABLE_DEADLINE_S", 0.3):
                    start = time.monotonic()
                    with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                        trail.log_decision("durable_but_bounded", tenant="t1")
                    self.assertLess(time.monotonic() - start, 2.0)

            self.assertIsNotNone(capture_metrics_snapshot(source="merge", tenant="t1"))
            # Once the store is initialized, a held init lock no longer matters at all.
            with _flock_held_by_child(db.with_name(db.name + ".lock")):
                start = time.monotonic()
                self.assertIsNotNone(capture_metrics_snapshot(source="merge", tenant="t1"))
                self.assertLess(time.monotonic() - start, 2.0)
            self.assertEqual(len(trail.query_metric_snapshots()), 2)

    def test_sqlite_exclusive_lock_degrades_capture_within_budget(self):
        from factonio import trail
        from factonio.metrics import capture_metrics_snapshot

        with isolated_hermes_home() as home:
            _seed_kanban_db(home / "kanban.db", time.time())
            wal = home / "wal" / "trail.db"
            trail.log_decision("seed", db_path=wal)
            rollback = home / "rollback" / "trail.db"
            _make_rollback_v1_store(rollback)
            for db in (wal, rollback):
                with _exclusive_txn(db), self.assertLogs("factonio.metrics", level="WARNING"):
                    start = time.monotonic()
                    snap = capture_metrics_snapshot(home / "kanban.db", source="merge", trail_db=db)
                    elapsed = time.monotonic() - start
                self.assertIsNone(snap, db)
                self.assertLess(elapsed, 2.0, db)
                self.assertIsNotNone(capture_metrics_snapshot(home / "kanban.db", source="merge", trail_db=db), db)

    def test_snapshot_write_runs_a_single_retry_loop(self):
        from factonio import trail

        with isolated_hermes_home() as home:
            db = home / "rollback" / "trail.db"
            _make_rollback_v1_store(db)
            real_open = trail._open
            calls = []

            def spy(path):
                calls.append(path)
                return real_open(path)

            with _exclusive_txn(db), mock.patch.object(trail, "_BUSY_TIMEOUT_MS", 5), \
                    mock.patch.object(trail, "_open", side_effect=spy):
                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    trail.record_metric_snapshot(_snapshot_metrics(), db_path=db)
                self.assertEqual(len(calls), trail._RETRY_ATTEMPTS)
                calls.clear()
                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    trail.log_decision("blocked", db_path=db)
                self.assertEqual(len(calls), trail._RETRY_ATTEMPTS)
            self.assertEqual(trail.record_metric_snapshot(_snapshot_metrics(), db_path=db)["source"], "manual")


class TestHistoryContractFixes(unittest.TestCase):
    def test_history_summary_reports_snapshot_count_independent_of_limit(self):
        from factonio import trail

        with isolated_hermes_home() as home, _no_worker_env():
            _register(home, "acme/api")
            client = _api_client("factonio_test_history_count")
            base = "/api/plugins/factonio/metrics/history"
            run = _cli()
            self.assertEqual(client.get(base).json()["summary"]["snapshot_count"], 0)
            self.assertEqual(trail.metrics_history_payload()["summary"]["snapshot_count"], 0)
            code, out, _ = run("metrics", "--history", "--json")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["summary"]["snapshot_count"], 0)

            for i in range(5):
                trail.record_metric_snapshot(_snapshot_metrics(cards_total=i), source="merge", tenant="t1")
            trail.record_metric_snapshot(_snapshot_metrics(), source="retro", tenant="t2")

            api = client.get(base, params={"tenant": "t1", "limit": 2}).json()
            self.assertEqual(len(api["snapshots"]), 2)
            self.assertEqual((api["summary"]["snapshot_count"], api["summary"]["total"]), (5, 5))
            payload = trail.metrics_history_payload(tenant="t1", limit=2)
            self.assertEqual((len(payload["snapshots"]), payload["summary"]["snapshot_count"]), (2, 5))
            self.assertEqual(trail.metrics_history_payload(limit=2)["summary"]["snapshot_count"], 6)
            code, out, _ = run("metrics", "--history", "--json", "--tenant", "t1", "--limit", "2")
            self.assertEqual(code, 0)
            cli = json.loads(out)
            self.assertEqual((len(cli["snapshots"]), cli["summary"]["snapshot_count"]), (2, 5))

    def test_out_of_range_iso_offsets_are_rejected_cleanly(self):
        from factonio import trail

        boundary = ("0001-01-01T00:00:00+02:00", "9999-12-31T23:59:59-02:00")
        with isolated_hermes_home() as home, _no_worker_env():
            _register(home, "acme/api")
            trail.record_metric_snapshot(_snapshot_metrics())
            client = _api_client("factonio_test_history_overflow")
            run = _cli()
            for since in (*boundary, "99999999999d"):
                with self.assertRaisesRegex(ValueError, "Invalid since"):
                    trail.resolve_since(since)
                res = client.get("/api/plugins/factonio/metrics/history", params={"since": since})
                self.assertEqual(res.status_code, 422, since)
                self.assertIn("Invalid since", res.json()["detail"])
                code, out, err = run("metrics", "--history", "--since", since)
                self.assertEqual(code, 1, since)
                self.assertIn("Invalid since", err)
                self.assertNotIn("Traceback", out + err)
                code, out, _ = run("metrics", "--history", "--since", since, "--json")
                self.assertEqual(code, 1)
                self.assertFalse(json.loads(out)["ok"])
            for ts in boundary:
                with self.assertRaisesRegex(ValueError, "Invalid ts"):
                    trail.record_metric_snapshot(_snapshot_metrics(), ts=ts)
            self.assertEqual(trail.resolve_since("0001-01-01T00:00:00Z"), "0001-01-01T00:00:00.000000Z")
            self.assertEqual(len(trail.query_metric_snapshots()), 1)


if __name__ == "__main__":
    unittest.main()
