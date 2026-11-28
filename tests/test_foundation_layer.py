"""Unit tests for the Layer 0 foundation modules and the extracted `verification` projections."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests._env import PLUGIN_ROOT, isolated_hermes_home

if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

SHA40 = "a" * 40
SHA64 = "b" * 64


class TestVocabulary(unittest.TestCase):
    def test_trail_phases_are_the_13_recordable_phases(self):
        from factonio.vocabulary import TRAIL_PHASES

        self.assertEqual(
            TRAIL_PHASES,
            ("define", "design", "implement", "verify", "publish", "gate", "merge", "intake", "harness", "eval",
             "escape", "watchdog", "retro"),
        )
        self.assertEqual(len(set(TRAIL_PHASES)), 13)

    def test_legacy_modules_re_export_the_same_objects(self):
        from factonio import arena, config, eval_skills, harness, issues, repos, trail, vocabulary

        pairs = [
            (config.WORKSTREAM_KINDS, vocabulary.WORKSTREAM_KINDS),
            (config.PUBLISH_POLICIES, vocabulary.PUBLISH_POLICIES),
            (config._VENDOR_ALIASES, vocabulary.VENDOR_ALIASES),
            (config._ROUTER_PROVIDERS, vocabulary.ROUTER_PROVIDERS),
            (eval_skills._VENDOR_ALIASES, vocabulary.VENDOR_ALIASES),
            (harness.HOOK_KINDS, vocabulary.HOOK_KINDS),
            (arena.ARENA_TARGETS, vocabulary.ARENA_TARGETS),
            (issues.FIX_VERDICTS, vocabulary.FIX_VERDICTS),
            (issues.TRIAGE_ACTIONS, vocabulary.TRIAGE_ACTIONS),
            (repos.WORKTREE_BUCKETS, vocabulary.WORKTREE_BUCKETS),
            (trail.TRAIL_SIGNALS, vocabulary.TRAIL_SIGNALS),
            (trail.AUDIT_FINDING_KINDS, vocabulary.AUDIT_FINDING_KINDS),
            (trail.RECALL_CATEGORIES, vocabulary.RECALL_CATEGORIES),
            (trail._GATE_LOOSENING_TOKENS, vocabulary.GATE_LOOSENING_TOKENS),
            (trail.scrub_public, vocabulary.scrub_public),
            (trail.contains_holdout_reference, vocabulary.contains_holdout_reference),
        ]
        for legacy, canonical in pairs:
            self.assertIs(legacy, canonical)

    def test_holdout_detection_and_public_scrub(self):
        from factonio.vocabulary import contains_holdout_reference, redact_holdout_text, scrub_public

        for text in ("runs/x/holdout/case.md", "HOLD-OUT", "ho\x1b[31mldout/a", "h\u200bold\u200bout", "hol\tdout/x"):
            self.assertTrue(contains_holdout_reference(text), repr(text))
        for text in ("hold out for review", "", None, "household"):
            self.assertFalse(contains_holdout_reference(text), repr(text))
        self.assertEqual(redact_holdout_text("see holdout/a b.md now"), "[redacted]")
        self.assertEqual(
            scrub_public({"holdout_path": "x", "why": "read holdout/a", "n": float("inf"), "ok": ["plain"]}),
            {"why": "[redacted]", "n": None, "ok": ["plain"]},
        )

    def test_gate_loosening_hint_scrub(self):
        from factonio.vocabulary import norm_token_text, scrub_loosening_hints

        self.assertEqual(norm_token_text("Skip Test-now"), "skip_test_now")
        why = "PR #4 is stacked; pass allow_stacked_merge=True to override, or merge the parent first"
        self.assertEqual(scrub_loosening_hints(why, "fallback"), "PR #4 is stacked; merge the parent first")
        self.assertEqual(scrub_loosening_hints("use bypass gate", "PR gate blocked"), "PR gate blocked")


class TestValidators(unittest.TestCase):
    def test_full_sha_regex_accepts_40_or_64_hex_only(self):
        from factonio.validators import FULL_SHA_RE, is_full_sha, validate_full_sha

        for good in (SHA40, SHA64, SHA40.upper()):
            self.assertTrue(FULL_SHA_RE.fullmatch(good))
            self.assertTrue(is_full_sha(good))
        for bad in ("a" * 39, "a" * 41, "a" * 63, "a" * 65, "g" * 40, "", " " + SHA40, 40):
            self.assertFalse(is_full_sha(bad), repr(bad))
        self.assertEqual(validate_full_sha(f"  {SHA40.upper()} "), SHA40)
        with self.assertRaisesRegex(ValueError, "head_sha must be a full 40- or 64-character"):
            validate_full_sha("abc1234", "head_sha")

    def test_issue_intake_accepts_64_hex_shas_consistently(self):
        from factonio.issues import _validate_head_sha

        self.assertEqual(_validate_head_sha(SHA64.upper()), SHA64)
        with self.assertRaisesRegex(ValueError, "head_sha"):
            _validate_head_sha("a" * 41)

    def test_normalize_repo_and_tenants(self):
        from factonio import repos, validators

        self.assertIs(repos.normalize_repo, validators.normalize_repo)
        for spec in ("Omnim-AI/Factonio", "https://github.com/Omnim-AI/Factonio.git", "git@github.com:Omnim-AI/Factonio"):
            self.assertEqual(validators.normalize_repo(spec), ("Omnim-AI/Factonio", "omnim-ai-factonio"))
        with self.assertRaises(ValueError):
            validators.normalize_repo("not a repo")
        self.assertEqual(validators.make_tenant("Acme_API", issue_number=7), "acme-api-i7")
        self.assertEqual(validators.make_tenant("acme", label="Fix: the Bug!"), "acme-fix-the-bug")
        self.assertEqual(validators.safe_tenant_dirname("A/B..c"), "a-b-c")
        self.assertEqual(validators.safe_tenant_dirname(""), "default")
        self.assertEqual(validators.validate_tenant_slug("acme-1.x"), "acme-1.x")
        for bad in ("../x", "a/b", "", "holdout-x", 3):
            with self.assertRaisesRegex(ValueError, "tenant must match"):
                validators.validate_tenant_slug(bad)

    def test_branch_pr_and_string_validators(self):
        from factonio.validators import (
            require_clean_str,
            single_line,
            stamp_token,
            validate_branch_field,
            validate_default_branch,
            validate_pr_number,
        )

        self.assertEqual(validate_default_branch(" main "), "main")
        with self.assertRaisesRegex(ValueError, "^branch must not contain '..'"):
            validate_branch_field("a..b", "branch")
        for bad in (0, -1, True, 1.5, "x"):
            with self.assertRaises(ValueError):
                validate_pr_number(bad)
        self.assertEqual(validate_pr_number("12"), 12)
        with self.assertRaisesRegex(ValueError, "NUL"):
            require_clean_str("a\x00b", "title")
        with self.assertRaisesRegex(ValueError, "UTF-8"):
            require_clean_str("bad \ud800", "title")
        require_clean_str(None, "title", optional=True)
        self.assertEqual(single_line("a\n\tb\u2028c"), "a b c")
        self.assertEqual(stamp_token("x`y", "d"), "x'y")
        self.assertEqual(stamp_token(None, "unset"), "unset")

    def test_git_ref_name_uses_the_injected_runner(self):
        from factonio.validators import validate_git_ref_name

        calls: list[list[str]] = []

        def runner(args, *, cwd):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0 if "good" in args[-1] else 1, "", "")

        validate_git_ref_name(Path("."), "good", "branch", run_git=runner)
        self.assertEqual(calls, [["check-ref-format", "refs/heads/good"], ["check-ref-format", "--branch", "good"]])
        for bad in ("@", "HEAD"):
            with self.assertRaisesRegex(ValueError, "is not a valid branch name"):
                validate_git_ref_name(Path("."), bad, "branch", run_git=lambda a, *, cwd: subprocess.CompletedProcess(a, 0))


class TestGitCli(unittest.TestCase):
    def test_injectable_runner_and_read_only_flag(self):
        from factonio import git_cli

        seen: list[tuple[list[str], dict]] = []

        def runner(argv, **kwargs):
            seen.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, "out", "")

        with git_cli.use_runner(runner):
            git_cli.run_git_ro(["status"], cwd="/tmp")
            git_cli.run_gh(["pr", "view", "1"])
            git_cli.run_git(["apply"], cwd=Path("/tmp"), input_text="patch", timeout=5)
        self.assertEqual([argv for argv, _ in seen],
                         [["git", "--no-optional-locks", "status"], ["gh", "pr", "view", "1"], ["git", "apply"]])
        self.assertEqual(seen[0][1]["cwd"], "/tmp")
        self.assertIsNone(seen[1][1]["cwd"])
        self.assertEqual((seen[2][1]["input"], seen[2][1]["timeout"], seen[2][1]["check"]), ("patch", 5, False))
        explicit = git_cli.run_gh(["auth"], runner=lambda argv, **kw: subprocess.CompletedProcess(argv, 3, "", "x"))
        self.assertEqual(explicit.returncode, 3)

    def test_capture_errors_never_raises(self):
        from factonio import git_cli

        def boom(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 1)

        with self.assertRaises(subprocess.TimeoutExpired):
            git_cli.run_git(["log"], cwd=".", runner=boom)
        proc = git_cli.run_git_ro(["log"], cwd=".", runner=boom, capture_errors=True)
        self.assertEqual(proc.returncode, 128)
        self.assertIn("timed out", proc.stderr)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(git_cli.is_git_repo(Path(tmp) / "missing"))

    def test_repos_seams_still_route_through_patchable_wrappers(self):
        from factonio import repos

        fake = mock.Mock(return_value=subprocess.CompletedProcess([], 1, "", "nope"))
        with mock.patch("factonio.repos._run_gh", fake):
            info = repos.inspect_github_repo("o/r")
        self.assertTrue(fake.called)
        self.assertEqual(info.get("default_branch"), "main")

    def test_parse_status_z_skips_rename_sources(self):
        from factonio import git_cli, repos

        self.assertIs(repos.parse_status_z, git_cli.parse_status_z)
        text = "R  new.txt\0old.txt\0 M a b.py\0?? x\0!! build/\0"
        self.assertEqual(git_cli.parse_status_z(text), [("R ", "new.txt"), (" M", "a b.py"), ("??", "x"), ("!!", "build/")])

    def test_git_err_formats_detail(self):
        from factonio.git_cli import git_err

        self.assertEqual(git_err(subprocess.CompletedProcess([], 2, "", " bad \n"), "fetch"), "git fetch failed: bad")
        self.assertEqual(git_err(subprocess.CompletedProcess([], 2, "", ""), "fetch"), "git fetch failed: exit 2")


def _make_db(path: Path, rows: int = 0) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE items (id TEXT PRIMARY KEY, v INTEGER)")
    conn.executemany("INSERT INTO items VALUES (?, ?)", [(f"t{i}", i) for i in range(rows)])
    conn.commit()
    conn.close()


class TestStoreKanban(unittest.TestCase):
    def test_read_only_uri_survives_question_mark_and_hash_in_path(self):
        from factonio.store_kanban import open_kanban_ro

        with tempfile.TemporaryDirectory() as tmp:
            weird = Path(tmp) / "board?x=1#frag dir"
            weird.mkdir()
            db = weird / "kanban.db"
            _make_db(db, rows=3)
            conn = open_kanban_ro(db)
            self.assertIsNotNone(conn)
            assert conn is not None
            with conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"], 3)
                with self.assertRaises(sqlite3.OperationalError):
                    conn.execute("INSERT INTO items VALUES ('z', 9)")
            conn.close()
            self.assertEqual(sorted(p.name for p in weird.iterdir()), ["kanban.db"], "nothing created beside the db")

    def test_absent_and_non_regular_paths(self):
        from factonio.store_kanban import open_kanban_ro

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNone(open_kanban_ro(root / "missing.db"))
            self.assertIsNone(open_kanban_ro(root / "missing.db", strict=True))
            self.assertFalse((root / "missing.db").exists())
            self.assertIsNone(open_kanban_ro(root), "lenient mode treats a directory as absent")
            with self.assertRaisesRegex(sqlite3.OperationalError, "is not a regular file"):
                open_kanban_ro(root, strict=True)
            dangling = root / "dangling.db"
            os.symlink(root / "nowhere.db", dangling)
            with self.assertRaisesRegex(sqlite3.OperationalError, "cannot stat"):
                open_kanban_ro(dangling, strict=True)
            garbage = root / "garbage.db"
            garbage.write_bytes(b"not sqlite" * 50)
            conn = open_kanban_ro(garbage, strict=True)
            assert conn is not None
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("SELECT * FROM sqlite_master").fetchall()
            conn.close()

    def test_chunked_in_query_handles_more_than_500_ids(self):
        from factonio.store_kanban import chunked_in_query

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "k.db"
            _make_db(db, rows=1300)
            conn = sqlite3.connect(db)
            statements: list[str] = []
            conn.set_trace_callback(statements.append)
            ids = [f"t{i}" for i in range(1200)] + ["missing"]
            rows = chunked_in_query(
                conn, "SELECT id FROM items WHERE v >= ? AND id IN ({placeholders}) AND v < ?", ids,
                prefix_params=(10,), suffix_params=(1100,),
            )
            self.assertEqual(sorted(r[0] for r in rows), sorted(f"t{i}" for i in range(10, 1100)))
            self.assertEqual(len([s for s in statements if s.startswith("SELECT")]), 3)
            self.assertEqual(chunked_in_query(conn, "SELECT id FROM items WHERE id IN ({placeholders})", []), [])
            with self.assertRaises(ValueError):
                chunked_in_query(conn, "SELECT 1 WHERE 1 IN ({placeholders})", [1], chunk_size=0)
            conn.close()

    def test_resolve_kanban_db_path_honors_pinned_env(self):
        from factonio.store_kanban import resolve_kanban_db_path

        with isolated_hermes_home() as home:
            pinned = home / "pinned" / "board.db"
            with mock.patch.dict(os.environ, {"HERMES_KANBAN_DB": str(pinned)}):
                self.assertEqual(resolve_kanban_db_path(), pinned)
                with mock.patch.dict(sys.modules, {"hermes_cli.kanban_db": None}):
                    self.assertEqual(resolve_kanban_db_path(), pinned)
            with mock.patch.dict(sys.modules, {"hermes_cli.kanban_db": None}):
                os.environ.pop("HERMES_KANBAN_DB", None)
                self.assertEqual(resolve_kanban_db_path(), home / "kanban.db")

    def test_table_columns_and_select_list(self):
        from factonio.store_kanban import select_list, table_columns

        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t (a, b)")
        self.assertEqual(table_columns(conn, "t"), {"a", "b"})
        self.assertEqual(table_columns(conn, "missing"), set())
        self.assertEqual(select_list(("a", "c"), {"a", "b"}), "a, NULL AS c")
        conn.close()


class TestVerificationExtract(unittest.TestCase):
    def test_metrics_and_orchestrator_re_export_verification(self):
        from factonio import metrics, orchestrator, verification

        for name in ("classify_phase", "parse_panel_tag", "parse_arena_tag", "parse_workstream_mode",
                     "aggregate_panel_verdicts", "summarize_panel", "load_panel_lanes", "expected_panel_lane_ids",
                     "local_branch_verification_status", "card_targets", "extract_local_target",
                     "local_target_from_metadata", "card_verdict"):
            self.assertIs(getattr(metrics, name), getattr(verification, name), name)
        self.assertIs(orchestrator.aggregate_panel_verdicts, verification.aggregate_panel_verdicts)
        self.assertIs(metrics._card_targets, verification.card_targets)
        for name in ("run_data_dir", "spec_file_path", "holdout_dir_path", "design_dir_path", "investigation_report_path"):
            self.assertIs(getattr(orchestrator, name), getattr(__import__("factonio.config", fromlist=[name]), name))

    def test_tags_targets_and_verdicts(self):
        from factonio.verification import (
            card_targets,
            card_verdict,
            classify_phase,
            local_target_from_metadata,
            parse_arena_tag,
            parse_panel_tag,
        )

        body = (
            "- Repository: `o/r`\n"
            f"- Local branch target: `feat/x` (base=`main`, head=`{SHA40}`)\n"
            "- Verification panel: `vp_ab12` (rigor=`high`, vendor=`openai`, model=`gpt-5`)\n"
        )
        task = {"title": "[verify:diff] o/r@feat/x", "skills": json.dumps(["factonio:verify-diff"]), "body": body}
        self.assertEqual(classify_phase(task), "verify-diff")
        self.assertEqual(parse_panel_tag(body), {"panel_id": "vp_ab12", "rigor": "high", "vendor": "openai", "model": "gpt-5"})
        pr, local = card_targets(task, [])
        self.assertIsNone(pr)
        self.assertEqual((local["branch"], local["head_sha"], local["repo"], local["source"]), ("feat/x", SHA40, "o/r", "card"))
        upper = body.replace(SHA40, SHA40.upper())
        self.assertIsNone(card_targets({**task, "body": upper}, [])[1], "stamps are written lowercase")
        self.assertEqual(card_verdict("verify-diff", "done", []), "pass")
        self.assertEqual(card_verdict("verify-diff", "done", [{"outcome": "changes_requested"}]), "reject")
        meta = {"branch": "b", "head_sha": SHA64.upper()}
        self.assertEqual(local_target_from_metadata(meta)["head_sha"], SHA64)
        arena = "- Arena: `ar_abcdef` (role=`judge`, target=`holdout`, vendor=`x`, model=`m`)"
        self.assertEqual(parse_arena_tag(arena)["target"], None)

    def test_chunked_metrics_runs_query_over_many_cards(self):
        from factonio.metrics import compute_metrics

        with isolated_hermes_home() as home:
            db = home / "big.db"
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE tasks (id TEXT, title TEXT, assignee TEXT, status TEXT, tenant TEXT, skills TEXT, "
                "created_at REAL, completed_at REAL, consecutive_failures INTEGER, result TEXT, body TEXT)"
            )
            conn.execute(
                "CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, started_at REAL, ended_at REAL, "
                "outcome TEXT, metadata TEXT)"
            )
            now = time.time()
            n = 1100
            conn.executemany(
                "INSERT INTO tasks VALUES (?, ?, 'factonio', 'done', 'ten', ?, ?, ?, 0, NULL, '')",
                [(f"t{i}", f"[implement] {i}", json.dumps(["factonio:implement"]), now - 100, now - 50) for i in range(n)],
            )
            conn.executemany(
                "INSERT INTO task_runs (task_id, started_at, ended_at, outcome, metadata) VALUES (?, ?, ?, 'completed', '{}')",
                [(f"t{i}", now - 90, now - 60) for i in range(n)] + [("t0", now - 59, now - 55)],
            )
            conn.commit()
            conn.close()
            result = compute_metrics(db, range_key="all", trail_db=home / "trail.db", now=now)
        self.assertEqual(result["phases"]["implement"]["count"], n)
        self.assertEqual(result["rework"]["cards_with_rework"], 1)
        self.assertEqual(result["rework"]["avg_runs_per_implement_card"], round((n + 1) / n, 2))


def _seed_board(db_path: Path, now: float, count: int) -> None:
    from hermes_cli.kanban_db_connect import connect_closing

    with connect_closing(db_path=db_path) as conn:
        for i in range(count):
            tid = f"t_{i:04d}"
            status = "blocked" if i % 2 else "running"
            conn.execute(
                "INSERT INTO tasks (id, title, assignee, status, tenant, skills, created_at, started_at, "
                "consecutive_failures, worker_pid, result) VALUES (?, ?, 'factonio', ?, 'acme', ?, ?, ?, ?, ?, 'r')",
                (tid, f"[implement] {i}", status, json.dumps(["factonio:implement"]), now - 7200, now - 7200,
                 5 if status == "blocked" else 0, 4242),
            )
            run = conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, started_at, worker_pid) "
                "VALUES (?, 'factonio', 'running', ?, 4242)",
                (tid, now - 7200),
            ).lastrowid
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, 'heartbeat', NULL, ?)",
                (tid, run, now - 60),
            )
            for j in range(10):
                conn.execute(
                    "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, 'human', ?, ?)",
                    (tid, f"c{j}", now - 7000 + j),
                )
        conn.commit()


class TestWatchdogBatchedQueries(unittest.TestCase):
    def _scan_counting(self, db: Path, now: float) -> tuple[list[dict], int]:
        from factonio import store_kanban, watchdog

        statements: list[str] = []
        real_open = store_kanban.open_kanban_ro

        def traced(path, **kwargs):
            conn = real_open(path, **kwargs)
            if conn is not None:
                conn.set_trace_callback(statements.append)
            return conn

        with mock.patch.object(watchdog, "open_kanban_ro", traced):
            findings = watchdog.scan_stuck_cards(
                db, now=now, pid_alive_fn=lambda pid: False, runs_root=db.parent / "no-runs"
            )
        return findings, len([s for s in statements if s.lstrip().upper().startswith("SELECT")])

    def test_query_count_is_independent_of_card_count(self):
        now = time.time()
        with isolated_hermes_home() as home:
            small, big = home / "small.db", home / "big.db"
            _seed_board(small, now, 4)
            _seed_board(big, now, 40)
            small_findings, small_queries = self._scan_counting(small, now)
            big_findings, big_queries = self._scan_counting(big, now)
        self.assertGreater(small_queries, 0, "the trace hook saw the scan's queries")
        self.assertEqual(small_queries, big_queries, "per-card N+1 queries are gone")
        self.assertEqual(len(big_findings), 40)
        kinds = {f["task_id"]: f["kind"] for f in big_findings}
        self.assertEqual(kinds["t_0000"], "dead_worker")
        self.assertEqual(kinds["t_0001"], "fix_cap_reached")
        self.assertEqual({f["run_id"] is not None for f in big_findings}, {True})

    def test_batched_comments_keep_newest_first_window(self):
        from factonio import watchdog

        now = time.time()
        with isolated_hermes_home() as home:
            db = home / "k.db"
            _seed_board(db, now, 2)
            conn = watchdog.open_kanban_ro(db)
            assert conn is not None
            with conn:
                activity = watchdog._batched_card_activity(
                    conn, ["t_0000", "t_0001"], running_ids=["t_0000"],
                    comment_cols=watchdog.table_columns(conn, "task_comments"),
                    run_select="id, status", event_cols=watchdog.table_columns(conn, "task_events"),
                )
            conn.close()
        self.assertEqual([c["body"] for c in activity["comments"]["t_0000"]], [f"c{j}" for j in range(9, 1, -1)])
        self.assertEqual(set(activity["runs"]), {"t_0000", "t_0001"})
        self.assertNotIn("task_id_", activity["runs"]["t_0000"])
        self.assertIsNone(activity["event_ts"].get("t_0000"), "heartbeats are not progress")
        self.assertNotIn("t_0001", activity["progress_event_ts"])
        self.assertAlmostEqual(activity["last_event"]["t_0001"], now - 60, places=3)


if __name__ == "__main__":
    unittest.main()
