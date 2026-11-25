"""Clean-room tests for watchdog PID/side-effect liveness, phase thresholds, and bounded triage."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from tests._env import PLUGIN_ROOT, isolated_hermes_home

if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Factonio Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Factonio Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}


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


def _add_task(
    db_path: Path,
    tid: str,
    *,
    now: float,
    skill: str = "factonio:implement",
    started_ago: float = 3600,
    heartbeat_ago: float | None = None,
    pid: int | None = None,
    claim_lock: str | None = None,
    max_runtime: int | None = None,
    workspace: str | None = None,
    tenant: str = "acme-api-i9",
    events: tuple[tuple[str, float], ...] = (),
    comments: tuple[tuple[str, str, float], ...] = (),
) -> None:
    from hermes_cli.kanban_db_connect import connect_closing

    beat = now - heartbeat_ago if heartbeat_ago is not None else None
    with connect_closing(db_path=db_path) as conn:
        conn.execute(
            """
            INSERT INTO tasks (id, title, assignee, status, tenant, session_id, skills, created_at,
                               started_at, last_heartbeat_at, worker_pid, claim_lock, max_runtime_seconds,
                               workspace_path)
            VALUES (?, ?, 'factonio', 'running', ?, 'sess', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (tid, f"[card] {tid}", tenant, json.dumps([skill]), now - started_ago, now - started_ago,
             beat, pid, claim_lock, max_runtime, workspace),
        )
        cur = conn.execute(
            """
            INSERT INTO task_runs (task_id, profile, status, started_at, last_heartbeat_at, worker_pid,
                                   claim_lock, max_runtime_seconds)
            VALUES (?, 'factonio', 'running', ?, ?, ?, ?, ?)
            """,
            (tid, now - started_ago, beat, pid, claim_lock, max_runtime),
        )
        run_id = cur.lastrowid
        for kind, ago in events:
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, ?, NULL, ?)",
                (tid, run_id, kind, now - ago),
            )
        for author, body, ago in comments:
            conn.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
                (tid, author, body, now - ago),
            )
        conn.commit()


def _scan(db_path: Path, now: float, **kwargs):
    from factonio.watchdog import scan_stuck_cards

    kwargs.setdefault("runs_root", db_path.parent / "no-runs")
    return {f["task_id"]: f for f in scan_stuck_cards(db_path, now=now, **kwargs)}


class _CountingTriage:
    def __init__(self, response) -> None:
        self.response = response
        self.calls: list[dict] = []

    def __call__(self, context: dict):
        self.calls.append(context)
        if isinstance(self.response, BaseException):
            raise self.response
        if callable(self.response):
            return self.response(context)
        return self.response


class TestPhaseThresholds(unittest.TestCase):
    def test_phase_defaults_floor_cap_and_overrides(self):
        from factonio.config import PHASE_STALE_HEARTBEAT_MINUTES, resolve_stale_heartbeat_minutes as r

        self.assertEqual(
            PHASE_STALE_HEARTBEAT_MINUTES,
            {"verify-diff": 10, "verify-blackbox": 15, "define": 20, "implement": 25, "design": 30},
        )
        for phase, minutes in PHASE_STALE_HEARTBEAT_MINUTES.items():
            self.assertEqual(r(phase), minutes)
            self.assertEqual(r(phase, stale_heartbeat_minutes=20), minutes)
        self.assertEqual(r("other"), 20)
        self.assertEqual(r(None), 20)
        # Floor when configured above the default.
        self.assertEqual(r("verify-diff", stale_heartbeat_minutes=40), 40)
        self.assertEqual(r("design", stale_heartbeat_minutes=25), 30)
        self.assertEqual(r("other", stale_heartbeat_minutes=40), 40)
        # Cap when configured below the default.
        self.assertEqual(r("design", stale_heartbeat_minutes=12), 12)
        self.assertEqual(r("verify-diff", stale_heartbeat_minutes=12), 10)
        # Per-phase override wins over both.
        self.assertEqual(r("implement", stale_heartbeat_minutes=40, phase_overrides={"implement": 5}), 5)
        self.assertEqual(r("implement", stale_heartbeat_minutes=12, phase_overrides={"design": 99}), 12)
        self.assertEqual(r("implement", phase_overrides={"implement": 0}), 25)


class TestWatchdogLiveness(unittest.TestCase):
    def setUp(self) -> None:
        self._llm = mock.patch("agent.auxiliary_client.call_llm", side_effect=AssertionError("LLM must not be called"))
        self.llm = self._llm.start()
        self.addCleanup(self._llm.stop)

    def test_is_pid_alive_real_processes(self):
        from factonio.watchdog import _is_pid_alive

        self.assertTrue(_is_pid_alive(os.getpid()))
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        self.assertFalse(_is_pid_alive(proc.pid))
        self.assertIsNone(_is_pid_alive(0))

    def test_dead_worker_is_immediate_and_deterministic(self):
        with isolated_hermes_home() as home:
            now = time.time()
            db = home / "kanban.db"
            # Fresh heartbeat and fresh side effects: only the dead PID can flag it.
            _add_task(db, "t_dead", now=now, started_ago=60, heartbeat_ago=5, pid=424242,
                      events=(("edited", 10),))
            triage = _CountingTriage({"classification": "working", "rationale": "x"})
            found = _scan(db, now, pid_alive_fn=lambda pid: False, triage_fn=triage)
            self.assertEqual(found["t_dead"]["kind"], "dead_worker")
            self.assertEqual(found["t_dead"]["worker_pid"], 424242)
            self.assertIn("424242", found["t_dead"]["reason"])
            self.assertEqual(triage.calls, [])

    def test_real_dead_pid_detected_without_injection(self):
        with isolated_hermes_home() as home:
            now = time.time()
            db = home / "kanban.db"
            proc = subprocess.Popen([sys.executable, "-c", "pass"])
            proc.wait()
            _add_task(db, "t_dead", now=now, started_ago=60, heartbeat_ago=5, pid=proc.pid)
            _add_task(db, "t_alive", now=now, started_ago=60, heartbeat_ago=5, pid=os.getpid())
            found = _scan(db, now)
            self.assertEqual(found["t_dead"]["kind"], "dead_worker")
            self.assertNotIn("t_alive", found)

    def test_remote_claim_host_pid_is_not_probed(self):
        with isolated_hermes_home() as home:
            now = time.time()
            db = home / "kanban.db"
            _add_task(db, "t_remote", now=now, started_ago=60, heartbeat_ago=5, pid=4242,
                      claim_lock="some-other-host.invalid:999")
            probe = mock.Mock(return_value=False)
            found = _scan(db, now, pid_alive_fn=probe)
            self.assertNotIn("t_remote", found)
            probe.assert_not_called()

    def test_runtime_exceeded_before_side_effects(self):
        with isolated_hermes_home() as home:
            now = time.time()
            db = home / "kanban.db"
            _add_task(db, "t_long", now=now, started_ago=7200, heartbeat_ago=5, pid=1234, max_runtime=3600,
                      events=(("edited", 5),))
            triage = _CountingTriage({"classification": "working", "rationale": "x"})
            found = _scan(db, now, pid_alive_fn=lambda pid: True, triage_fn=triage)
            self.assertEqual(found["t_long"]["kind"], "runtime_exceeded")
            self.assertEqual(triage.calls, [])

    def test_phase_aware_thresholds_in_scan(self):
        with isolated_hermes_home() as home:
            now = time.time()
            db = home / "kanban.db"
            for tid, skill in (("t_vd", "factonio:verify-diff"), ("t_vb", "factonio:verify-blackbox"),
                               ("t_def", "factonio:define"), ("t_imp", "factonio:implement"),
                               ("t_des", "factonio:design")):
                _add_task(db, tid, now=now, skill=skill, started_ago=22 * 60, heartbeat_ago=22 * 60)
            found = _scan(db, now)
            self.assertEqual(set(found), {"t_vd", "t_vb", "t_def"})
            self.assertEqual(found["t_vd"]["threshold_minutes"], 10)
            self.assertEqual(found["t_vd"]["phase"], "verify-diff")
            self.assertEqual(found["t_vd"]["kind"], "stale_running")
            self.assertEqual(found["t_vd"]["stall_signal"], "stale_heartbeat")

            # A configured floor of 40m silences all of them; a per-phase override re-enables one.
            self.assertEqual(_scan(db, now, stale_heartbeat_minutes=40), {})
            found = _scan(db, now, stale_heartbeat_minutes=40, phase_overrides={"design": 21})
            self.assertEqual(set(found), {"t_des"})

    def test_fresh_side_effects_suppress_stale_heartbeat(self):
        with isolated_hermes_home() as home:
            now = time.time()
            db = home / "kanban.db"
            _add_task(db, "t_event", now=now, started_ago=3600, heartbeat_ago=3000, pid=1,
                      events=(("heartbeat", 30), ("completed_step", 120)))
            _add_task(db, "t_comment", now=now, started_ago=3600, heartbeat_ago=3000, pid=1,
                      comments=(("factonio", "pushed fix", 60),))
            # Heartbeat events and watchdog comments are not side effects.
            _add_task(db, "t_beats", now=now, started_ago=3600, heartbeat_ago=3000, pid=1,
                      events=(("heartbeat", 30),), comments=(("factonio:watchdog", "alert", 10),))
            triage = _CountingTriage({"classification": "stalled", "rationale": "loop"})
            found = _scan(db, now, pid_alive_fn=lambda pid: True, triage_fn=triage)
            self.assertEqual(set(found), {"t_beats"})
            self.assertEqual([c["task_id"] for c in triage.calls], ["t_beats"])

    def test_workspace_git_and_run_dir_mtime_count_as_side_effects(self):
        with isolated_hermes_home() as home, mock.patch.dict(os.environ, GIT_ENV):
            now = time.time()
            db = home / "kanban.db"
            ws = home / "ws"
            ws.mkdir()
            subprocess.run(["git", "init", "-q", str(ws)], check=True)
            (ws / "a.txt").write_text("a", encoding="utf-8")
            subprocess.run(["git", "-C", str(ws), "add", "a.txt"], check=True)
            subprocess.run(["git", "-C", str(ws), "commit", "-qm", "a"], check=True)
            _add_task(db, "t_git", now=now, started_ago=3600, heartbeat_ago=3000, pid=1, workspace=str(ws))

            runs_root = home / "plugin-data" / "factonio" / "runs"
            (runs_root / "tenant-fresh").mkdir(parents=True)
            (runs_root / "tenant-fresh" / "spec.md").write_text("spec", encoding="utf-8")
            _add_task(db, "t_rundir", now=now, started_ago=3600, heartbeat_ago=3000, pid=1, tenant="tenant-fresh")

            stale_ws = home / "stale-ws"
            stale_ws.mkdir()
            (stale_ws / "old.txt").write_text("old", encoding="utf-8")
            old = now - 7200
            os.utime(stale_ws / "old.txt", (old, old))
            os.utime(stale_ws, (old, old))
            _add_task(db, "t_stale_ws", now=now, started_ago=3600, heartbeat_ago=3000, pid=1,
                      workspace=str(stale_ws))

            found = _scan(db, now, pid_alive_fn=lambda pid: True, runs_root=runs_root)
            self.assertEqual(set(found), {"t_stale_ws"})

            # Uncommitted edits to tracked files are tree activity too.
            future = now + 3600
            (ws / "a.txt").write_text("b", encoding="utf-8")
            os.utime(ws / "a.txt", (future - 60, future - 60))
            found = _scan(db, future, pid_alive_fn=lambda pid: True, runs_root=home / "none")
            self.assertNotIn("t_git", found)

    def test_fresh_nested_file_in_old_untracked_dir_is_a_side_effect(self):
        with isolated_hermes_home() as home, mock.patch.dict(os.environ, GIT_ENV):
            now = time.time()
            db = home / "kanban.db"
            ws = home / "ws"
            ws.mkdir()
            subprocess.run(["git", "init", "-q", str(ws)], check=True)
            (ws / "a.txt").write_text("a", encoding="utf-8")
            subprocess.run(["git", "-C", str(ws), "add", "a.txt"], check=True)
            old = now - 7200
            subprocess.run(
                ["git", "-C", str(ws), "commit", "-qm", "a"], check=True,
                env={**os.environ, "GIT_AUTHOR_DATE": f"@{int(old)}", "GIT_COMMITTER_DATE": f"@{int(old)}"},
            )
            newdir = ws / "newdir" / "deep"
            newdir.mkdir(parents=True)
            (newdir / "work.py").write_text("x = 1\n", encoding="utf-8")
            (ws / "newdir" / "old.txt").write_text("old", encoding="utf-8")
            for path in (ws / "a.txt", ws / "newdir" / "old.txt", newdir, ws / "newdir", ws / ".git" / "index"):
                os.utime(path, (old, old))
            # Fresh write to an existing nested file leaves both directory mtimes old.
            (newdir / "work.py").write_text("x = 2\n", encoding="utf-8")
            os.utime(newdir, (old, old))
            os.utime(ws / "newdir", (old, old))

            _add_task(db, "t_nested", now=now, started_ago=3600, heartbeat_ago=30, pid=1,
                      events=(("heartbeat", 30), ("claimed", 3600)), workspace=str(ws))
            triage = _CountingTriage({"classification": "stalled", "rationale": "loop"})
            found = _scan(db, now, pid_alive_fn=lambda pid: True, triage_fn=triage)
            self.assertEqual(found, {})
            self.assertEqual(triage.calls, [])

            from factonio.watchdog import _workspace_activity_ts

            self.assertGreaterEqual(_workspace_activity_ts(str(ws)), now - 60)

    def test_heartbeating_loop_without_side_effects(self):
        with isolated_hermes_home() as home:
            now = time.time()
            db = home / "kanban.db"
            _add_task(db, "t_loop", now=now, started_ago=3600, heartbeat_ago=30, pid=1,
                      events=(("heartbeat", 30), ("heartbeat", 330), ("claimed", 3600)))
            found = _scan(db, now, pid_alive_fn=lambda pid: True)
            self.assertEqual(found["t_loop"]["kind"], "no_side_effects")
            self.assertEqual(found["t_loop"]["stall_signal"], "no_side_effects")
            self.assertIsNone(found["t_loop"]["triage"])
            self.assertGreaterEqual(found["t_loop"]["side_effect_age_seconds"], 3500)

    def test_triage_classifications(self):
        with isolated_hermes_home() as home:
            now = time.time()
            db = home / "kanban.db"
            _add_task(db, "t_a", now=now, started_ago=3600, heartbeat_ago=3000, pid=1)

            working = _CountingTriage('```json\n{"classification": "working", "rationale": "long test run"}\n```')
            self.assertEqual(_scan(db, now, pid_alive_fn=lambda p: True, triage_fn=working), {})
            self.assertEqual(len(working.calls), 1)
            ctx = working.calls[0]
            self.assertEqual(ctx["task_id"], "t_a")
            self.assertEqual(ctx["stall_signal"], "stale_heartbeat")

            stalled = _CountingTriage({"classification": "stalled", "rationale": "same tool call repeated"})
            f = _scan(db, now, pid_alive_fn=lambda p: True, triage_fn=stalled)["t_a"]
            self.assertEqual(f["kind"], "stale_running")
            self.assertIn("same tool call repeated", f["reason"])
            self.assertEqual(f["triage"]["classification"], "stalled")

            needs = _CountingTriage({"classification": "needs_input", "rationale": "waiting on credentials"})
            f = _scan(db, now, pid_alive_fn=lambda p: True, triage_fn=needs)["t_a"]
            self.assertEqual(f["kind"], "triage_needs_input")
            self.assertIn("waiting on credentials", f["reason"])

    def test_triage_fallbacks_are_deterministic(self):
        with isolated_hermes_home() as home:
            now = time.time()
            db = home / "kanban.db"
            _add_task(db, "t_a", now=now, started_ago=3600, heartbeat_ago=3000, pid=1)
            for response in (RuntimeError("boom"), "not json", {"classification": "maybe"},
                             lambda ctx: time.sleep(2) or {"classification": "working"}):
                with self.subTest(response=repr(response)[:30]):
                    t0 = time.monotonic()
                    f = _scan(db, now, pid_alive_fn=lambda p: True,
                              triage_fn=_CountingTriage(response), triage_timeout_s=0.2)["t_a"]
                    self.assertLess(time.monotonic() - t0, 1.5)
                    self.assertEqual(f["kind"], "stale_running")
                    self.assertIsNone(f["triage"])

    def test_triage_cooldown(self):
        with isolated_hermes_home() as home:
            now = time.time()
            db = home / "kanban.db"
            _add_task(db, "t_a", now=now, started_ago=3600, heartbeat_ago=3000, pid=1)
            cooldowns: dict = {}
            triage = _CountingTriage({"classification": "working", "rationale": "compiling"})
            for offset in (0, 300, 600):
                self.assertEqual(
                    _scan(db, now + offset, pid_alive_fn=lambda p: True, triage_fn=triage,
                          triage_cooldowns=cooldowns), {},
                )
            self.assertEqual(len(triage.calls), 1)
            _scan(db, now + 16 * 60, pid_alive_fn=lambda p: True, triage_fn=triage, triage_cooldowns=cooldowns)
            self.assertEqual(len(triage.calls), 2)

            failing = _CountingTriage(RuntimeError("down"))
            cooldowns = {}
            for offset in (0, 60):
                f = _scan(db, now + offset, pid_alive_fn=lambda p: True, triage_fn=failing,
                          triage_cooldowns=cooldowns)
                self.assertEqual(f["t_a"]["kind"], "stale_running")
            self.assertEqual(len(failing.calls), 1)

    def test_no_llm_without_explicit_watchdog_model(self):
        with isolated_hermes_home() as home:
            now = time.time()
            db = home / "kanban.db"
            _add_task(db, "t_a", now=now, started_ago=3600, heartbeat_ago=3000, pid=1)
            (home / "config.yaml").write_text("model:\n  provider: anthropic\n  default: some-model\n", encoding="utf-8")
            f = _scan(db, now, pid_alive_fn=lambda p: True)["t_a"]
            self.assertEqual(f["kind"], "stale_running")
            self.assertIsNone(f["triage"])
            self.llm.assert_not_called()

    def test_explicit_repo_watchdog_model_uses_auxiliary_client(self):
        from factonio.config import save_repos_store

        with isolated_hermes_home() as home:
            now = time.time()
            db = home / "kanban.db"
            save_repos_store({"acme-api": {"repo": "acme/api", "path": str(home),
                                           "models": {"watchdog": {"provider": "openrouter", "model": "cheap-1"}}}})
            _add_task(db, "t_a", now=now, started_ago=3600, heartbeat_ago=3000, pid=1, tenant="acme-api-i7")
            _add_task(db, "t_other", now=now, started_ago=3600, heartbeat_ago=3000, pid=1, tenant="zzz-i1")
            self.llm.side_effect = None
            self.llm.return_value = "resp"
            with mock.patch(
                "agent.auxiliary_client.extract_content_or_reasoning",
                return_value='{"classification": "needs_input", "rationale": "asked a question"}',
            ):
                found = _scan(db, now, pid_alive_fn=lambda p: True)
            self.assertEqual(found["t_a"]["kind"], "triage_needs_input")
            self.assertEqual(found["t_other"]["kind"], "stale_running")
            self.assertEqual(self.llm.call_count, 1)
            kwargs = self.llm.call_args.kwargs
            self.assertEqual(kwargs["task"], "factonio_watchdog")
            self.assertEqual((kwargs["provider"], kwargs["model"]), ("openrouter", "cheap-1"))
            self.assertLessEqual(kwargs["timeout"], 15.0)
            self.assertLessEqual(kwargs["max_tokens"], 256)

    def test_tick_persists_cooldowns_and_ignores_watchdog_comments_for_dedupe(self):
        from hermes_cli.kanban_db_connect import connect_closing

        from factonio.watchdog import STATE_KEY_TRIAGE_COOLDOWNS, run_watchdog_tick

        with isolated_hermes_home() as home:
            now = time.time()
            db = home / "kanban.db"
            _add_task(db, "t_a", now=now, started_ago=3600, heartbeat_ago=3000, pid=1)
            ctx = _FakeCtx()
            triage = _CountingTriage({"classification": "stalled", "rationale": "idle"})
            kw = dict(db_path_override=db, pid_alive_fn=lambda p: True, triage_fn=triage,
                      runs_root=home / "none")
            first = run_watchdog_tick(ctx, now=now, **kw)
            self.assertEqual([a["kind"] for a in first["alerts"]], ["stale_running"])
            self.assertIn("t_a", ctx.state.get(STATE_KEY_TRIAGE_COOLDOWNS))

            with connect_closing(db_path=db) as conn:
                conn.execute(
                    "INSERT INTO task_comments (task_id, author, body, created_at) VALUES ('t_a', 'factonio:watchdog', ?, ?)",
                    (first["alerts"][0]["reason"], now + 1),
                )
                conn.commit()
            second = run_watchdog_tick(ctx, now=now + 360, **kw)
            self.assertTrue(second["ran"])
            self.assertEqual(second["alerts"], [])
            self.assertEqual(len(triage.calls), 1)

            with connect_closing(db_path=db) as conn:
                conn.execute(
                    "INSERT INTO task_comments (task_id, author, body, created_at) VALUES ('t_a', 'human', 'ping?', ?)",
                    (now - 3500,),
                )
                conn.commit()
            third = run_watchdog_tick(ctx, now=now + 720, **kw)
            self.assertEqual(len(third["alerts"]), 1)

    def test_dedup_is_kind_aware_so_escalation_alerts_once(self):
        from factonio.watchdog import run_watchdog_tick

        with isolated_hermes_home() as home:
            now = time.time()
            db = home / "kanban.db"
            _add_task(db, "t_a", now=now, started_ago=3600, heartbeat_ago=3000, pid=4242)
            ctx = _FakeCtx()
            alive = {"value": True}
            kw = dict(db_path_override=db, pid_alive_fn=lambda p: alive["value"], runs_root=home / "none")

            first = run_watchdog_tick(ctx, now=now, **kw)
            self.assertEqual([a["kind"] for a in first["alerts"]], ["stale_running"])

            alive["value"] = False
            second = run_watchdog_tick(ctx, now=now + 360, **kw)
            self.assertEqual([a["kind"] for a in second["alerts"]], ["dead_worker"])
            self.assertEqual([e[1]["kind"] for e in ctx.events], ["stale_running", "dead_worker"])

            third = run_watchdog_tick(ctx, now=now + 720, **kw)
            self.assertTrue(third["ran"])
            self.assertEqual(third["alerts"], [])
            self.assertEqual(len(ctx.events), 2)

    def test_dedup_realerts_for_a_new_run(self):
        from hermes_cli.kanban_db_connect import connect_closing

        from factonio.watchdog import run_watchdog_tick

        with isolated_hermes_home() as home:
            now = time.time()
            db = home / "kanban.db"
            _add_task(db, "t_a", now=now, started_ago=3600, heartbeat_ago=3000, pid=4242)
            ctx = _FakeCtx()
            kw = dict(db_path_override=db, pid_alive_fn=lambda p: False, runs_root=home / "none")
            self.assertEqual(len(run_watchdog_tick(ctx, now=now, **kw)["alerts"]), 1)
            self.assertEqual(run_watchdog_tick(ctx, now=now + 360, **kw)["alerts"], [])
            with connect_closing(db_path=db) as conn:
                conn.execute(
                    "INSERT INTO task_runs (task_id, profile, status, started_at, worker_pid)"
                    " VALUES ('t_a', 'factonio', 'running', ?, 4243)",
                    (now + 400,),
                )
                conn.commit()
            third = run_watchdog_tick(ctx, now=now + 720, **kw)
            self.assertEqual([a["kind"] for a in third["alerts"]], ["dead_worker"])

    def test_minimal_legacy_schema(self):
        with isolated_hermes_home() as home:
            now = time.time()
            db = home / "legacy.db"
            conn = sqlite3.connect(db)
            conn.executescript(
                """
                CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT,
                    tenant TEXT, created_at REAL, started_at REAL, result TEXT);
                CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, started_at REAL);
                """
            )
            conn.execute(
                "INSERT INTO tasks VALUES ('t_old', '[verify:diff] x/y#1', 'factonio', 'running', 'ws', ?, ?, NULL)",
                (now - 3600, now - 3600),
            )
            conn.execute(
                "INSERT INTO tasks VALUES ('t_blk', '[implement] y', 'factonio', 'blocked', 'ws', ?, ?, 'stuck')",
                (now - 7200, now - 7200),
            )
            conn.execute("INSERT INTO task_runs (task_id, started_at) VALUES ('t_old', ?)", (now - 3600,))
            conn.commit()
            conn.close()
            found = _scan(db, now)
            self.assertEqual(found["t_old"]["kind"], "stale_running")
            self.assertEqual(found["t_old"]["phase"], "verify-diff")
            self.assertIsNone(found["t_old"]["pid_alive"])
            self.assertEqual(found["t_blk"]["kind"], "blocked_waiting")


if __name__ == "__main__":
    unittest.main()
