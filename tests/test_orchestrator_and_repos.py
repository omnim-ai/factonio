"""Clean-room tests for repo registration, Kanban DAG creation, holdout barrier, and PR gate."""

from __future__ import annotations

import json
import subprocess
import sys
import time
import unittest
import warnings
from pathlib import Path
from unittest import mock

from tests._env import PLUGIN_ROOT, isolated_hermes_home

if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))


class TestOrchestratorAndRepos(unittest.TestCase):
    def test_normalize_repo(self):
        from factonio.repos import normalize_repo

        self.assertEqual(normalize_repo("tozes/factonio"), ("tozes/factonio", "tozes-factonio"))
        self.assertEqual(normalize_repo("https://github.com/Acme/My_App.git"), ("Acme/My_App", "acme-my-app"))
        self.assertEqual(normalize_repo("git@github.com:org/repo.git"), ("org/repo", "org-repo"))
        with self.assertRaises(ValueError):
            normalize_repo("not-a-repo")

    def test_workstream_and_isolated_verification_information_barrier(self):
        from hermes_cli import kanban_db as kb
        from hermes_cli.kanban_db_connect import connect_closing
        from factonio.orchestrator import spawn_implementation_card, spawn_verification_pair, start_workstream
        from factonio.repos import add_repo

        with isolated_hermes_home() as home:
            local_repo = home / "dev" / "sample"
            local_repo.mkdir(parents=True)
            (local_repo / ".git").mkdir()

            added = add_repo(
                "acme/sample",
                local_path=str(local_repo),
                clone=False,
                inspect_remote=False,
            )
            self.assertTrue(added["ok"])
            self.assertEqual(added["repo"]["merge_policy"], "auto")

            # 1. Start feature -> Definition card with goal_mode=True
            started = start_workstream(
                "acme/sample",
                "Add rate limiting to POST /v1/events",
                kind="feature",
                issue_number=42,
                session_id="sess_abc",
            )
            self.assertEqual(started["phase"], "define")
            self.assertEqual(started["tenant"], "acme-sample-i42")
            self.assertTrue(Path(started["holdout_dir"]).is_dir())

            # 2. Spawn implementation unit -> never references holdout
            impl = spawn_implementation_card(
                "acme/sample",
                started["tenant"],
                "Implement token bucket middleware",
                "Add per-key token bucket in src/ratelimit.py",
                parents=[started["task_id"]],
                session_id="sess_abc",
            )

            # 3. Spawn parallel isolated verification pair
            ver = spawn_verification_pair(
                "acme/sample",
                started["tenant"],
                pr_number=7,
                impl_card_id=impl["task_id"],
                head_sha="deadbeef" * 5,
                run_hint="pytest -q",
                session_id="sess_abc",
            )

            with connect_closing() as conn:
                def_task = kb.get_task(conn, started["task_id"])
                impl_task = kb.get_task(conn, impl["task_id"])
                diff_task = kb.get_task(conn, ver["verify_diff_task_id"])
                bb_task = kb.get_task(conn, ver["verify_blackbox_task_id"])

            assert def_task and impl_task and diff_task and bb_task
            self.assertTrue(def_task.goal_mode)
            self.assertEqual(def_task.skills, ["factonio:define"])
            self.assertEqual(impl_task.skills, ["factonio:implement"])
            self.assertEqual(diff_task.skills, ["factonio:verify-diff"])
            self.assertEqual(bb_task.skills, ["factonio:verify-blackbox"])

            # Strict information barrier assertions:
            # - Diff Check body MUST NOT contain the holdout directory path
            self.assertNotIn(started["holdout_dir"], diff_task.body or "")
            # - Implementation body MUST NOT contain the holdout directory path
            self.assertNotIn(started["holdout_dir"], impl_task.body or "")
            # - Black-Box Check body MUST contain holdout dir AND forbid reading git diff
            self.assertIn(started["holdout_dir"], bb_task.body or "")
            self.assertIn("NEVER run `git diff`", bb_task.body or "")

            # Auto-subscribe + Thread Cards assertions: every created card must subscribe
            # the calling session for notify+wake and resolve parent edges via task_links.
            self.assertTrue(started["subscribed"])
            self.assertTrue(impl["subscribed"])
            self.assertTrue(ver["subscribed"])
            from hermes_cli import kanban_db_notify as kbn
            from factonio.orchestrator import workstream_status

            with connect_closing() as conn:
                for tid in (started["task_id"], impl["task_id"], ver["verify_diff_task_id"], ver["verify_blackbox_task_id"]):
                    subs = kbn.list_notify_subs(conn, tid)
                    self.assertTrue(
                        any(s["chat_id"] == "sess_abc" and s["delivery_mode"] == "notify+wake" for s in subs),
                        f"Missing notify+wake subscription for {tid}: {subs}",
                    )
            ws = workstream_status(session_id="sess_abc")
            by_id = {c["id"]: c for c in ws["cards"]}
            self.assertEqual(len(by_id), 4)
            self.assertIn(started["task_id"], by_id[impl["task_id"]]["parents"])
            self.assertIn(impl["task_id"], by_id[ver["verify_diff_task_id"]]["parents"])
            self.assertIn(impl["task_id"], by_id[ver["verify_blackbox_task_id"]]["parents"])

    def test_phase_1b_design_provider_fallback_and_artifact_linkage(self):
        from hermes_cli import kanban_db as kb
        from hermes_cli.kanban_db_connect import connect_closing
        from factonio.metrics import classify_phase
        from factonio.orchestrator import (
            design_dir_path,
            spawn_design_card,
            spawn_implementation_card,
            spawn_verification_pair,
            start_workstream,
        )
        from factonio.repos import add_repo

        with isolated_hermes_home() as home:
            local_repo = home / "dev" / "uiapp"
            local_repo.mkdir(parents=True)
            (local_repo / ".git").mkdir()

            add_repo("acme/uiapp", local_path=str(local_repo), clone=False, inspect_remote=False)
            started = start_workstream(
                "acme/uiapp",
                "Redesign the dashboard cards and sidebar layout",
                kind="feature",
                issue_number=9,
                session_id="sess_ui",
            )
            tenant = started["tenant"]

            # 1. design_provider=off skips cleanly
            skipped = spawn_design_card("acme/uiapp", tenant, provider="off", session_id="sess_ui")
            self.assertFalse(skipped["ok"])
            self.assertTrue(skipped["skipped"])

            # 2. Strict claude-design fails fast when claude CLI/creds are absent
            missing_probe = {"claude_bin": None, "has_credentials": False, "available": False}
            with mock.patch("factonio.config.detect_claude_design_support", return_value=missing_probe):
                with self.assertRaises(ValueError):
                    spawn_design_card("acme/uiapp", tenant, provider="claude-design", session_id="sess_ui")

                # 3. auto falls back gracefully to pure-Hermes prototyping
                auto_res = spawn_design_card(
                    "acme/uiapp",
                    tenant,
                    title="Dashboard redesign",
                    brief="Clean card density and dark theme parity.",
                    provider="auto",
                    parents=[started["task_id"]],
                    session_id="sess_ui",
                )
            self.assertTrue(auto_res["ok"])
            self.assertTrue(auto_res["subscribed"])
            self.assertEqual(auto_res["design_provider"]["effective"], "hermes")

            # 4. auto selects claude-design when claude CLI + credentials exist
            present_probe = {"claude_bin": "/usr/local/bin/claude", "has_credentials": True, "available": True}
            with mock.patch("factonio.config.detect_claude_design_support", return_value=present_probe):
                cd_res = spawn_design_card(
                    "acme/uiapp",
                    tenant,
                    title="Claude Design pass",
                    provider="auto",
                    session_id="sess_ui",
                )
            self.assertTrue(cd_res["ok"])
            self.assertEqual(cd_res["design_provider"]["effective"], "claude-design")

            # 5. Once design/ has artifacts, Implement and Verification cards auto-link it without leaking holdout/
            ddir = design_dir_path(tenant)
            (ddir / "DESIGN.md").write_text("# Approved Design\n", encoding="utf-8")

            impl = spawn_implementation_card(
                "acme/uiapp",
                tenant,
                "Implement refreshed dashboard",
                "Apply approved design tokens.",
                parents=[cd_res["task_id"]],
                session_id="sess_ui",
            )
            ver = spawn_verification_pair(
                "acme/uiapp",
                tenant,
                pr_number=3,
                impl_card_id=impl["task_id"],
                session_id="sess_ui",
            )

            with connect_closing() as conn:
                des_task = kb.get_task(conn, cd_res["task_id"])
                imp_task = kb.get_task(conn, impl["task_id"])
                diff_task = kb.get_task(conn, ver["verify_diff_task_id"])
                bb_task = kb.get_task(conn, ver["verify_blackbox_task_id"])

            assert des_task and imp_task and diff_task and bb_task
            self.assertTrue(des_task.goal_mode)
            self.assertEqual(des_task.skills, ["factonio:design"])
            self.assertEqual(classify_phase({"skills": des_task.skills, "title": des_task.title}), "design")
            self.assertNotIn(started["holdout_dir"], des_task.body or "")
            self.assertIn(str(ddir), imp_task.body or "")
            self.assertNotIn(started["holdout_dir"], imp_task.body or "")
            self.assertIn(str(ddir), diff_task.body or "")
            self.assertNotIn(started["holdout_dir"], diff_task.body or "")
            self.assertIn(str(ddir), bb_task.body or "")
            self.assertIn(started["holdout_dir"], bb_task.body or "")

    def test_pr_gate_and_merge_refuse_red_or_stale_head(self):
        from factonio.repos import inspect_pr_gate, merge_pr

        def _graphql(conclusion: str) -> str:
            pr = {
                "number": 12,
                "url": "https://github.com/acme/sample/pull/12",
                "title": "Add retries",
                "state": "OPEN",
                "isDraft": False,
                "headRefName": "feat/retries",
                "headRefOid": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "baseRefName": "main",
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
                "reviewDecision": "APPROVED",
                "reviewThreads": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []},
                "commits": {"nodes": [{"commit": {"oid": "a" * 40, "statusCheckRollup": {
                    "state": conclusion,
                    "contexts": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [{"__typename": "CheckRun", "name": "ci", "status": "COMPLETED", "conclusion": conclusion}],
                    },
                }}}]},
            }
            return json.dumps({"data": {"repository": {"pullRequest": pr}}})

        green_payload = _graphql("SUCCESS")
        red_payload = _graphql("FAILURE")
        merged_readback = json.dumps({"state": "MERGED", "mergedAt": "2026-11-25T12:00:00Z", "baseRefName": "main"})

        def _proc(stdout: str, rc: int = 0):
            return subprocess.CompletedProcess(args=["gh"], returncode=rc, stdout=stdout, stderr="")

        with isolated_hermes_home():
            with mock.patch("factonio.repos._run_gh", return_value=_proc(red_payload)):
                gate = inspect_pr_gate("acme/sample", 12)
                self.assertTrue(gate["ok"])
                self.assertFalse(gate["ready_to_merge"])
                self.assertTrue(any("Failed checks" in b for b in gate["blockers"]))

            with mock.patch("factonio.repos._run_gh", return_value=_proc(green_payload)):
                stale = merge_pr("acme/sample", 12, expected_head_sha="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
                self.assertFalse(stale["merged"])
                self.assertIn("HEAD mismatch", stale["error"])

            with mock.patch(
                "factonio.repos._run_gh",
                side_effect=[_proc(green_payload), _proc(""), _proc(merged_readback)],
            ):
                merged = merge_pr("acme/sample", 12, expected_head_sha="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
                self.assertTrue(merged["ok"])
                self.assertTrue(merged["merged"])
                self.assertFalse(merged["queued"])


_CONFIG_REPO_YAML = """plugins:
  entries:
    factonio:
      settings:
        design_provider: hermes
        design_approval: ask
        repos:
          acme-legacy:
            slug: acme-legacy
            repo: acme/legacy
            url: https://github.com/acme/legacy
            path: /tmp/acme-legacy
            project_slug: acme-legacy
            default_branch: trunk
            merge_policy: ask
            max_fix_rounds: 2
            has_required_checks: true
            required_checks: [ci, lint]
            ci_contract: strict
            design_provider: "off"
            design_approval: auto
            models:
              implement: {provider: anthropic, model: claude-x}
"""


class TestRepoUpdate(unittest.TestCase):
    def _register(self, home: Path, spec: str = "acme/sample", **kw):
        from factonio.repos import add_repo

        local = home / "dev" / spec.split("/")[1]
        local.mkdir(parents=True, exist_ok=True)
        (local / ".git").mkdir(exist_ok=True)
        return add_repo(spec, local_path=str(local), clone=False, inspect_remote=False, **kw)

    def _store(self, home: Path) -> dict:
        return json.loads((home / "plugin-data" / "factonio" / "repos.json").read_text(encoding="utf-8"))

    def test_merge_policies_constant(self):
        from factonio.config import MERGE_POLICIES

        self.assertEqual(MERGE_POLICIES, ("auto", "ask"))

    def test_update_repo_updates_only_supplied_fields(self):
        from factonio.config import get_repo
        from factonio.repos import update_repo

        with isolated_hermes_home() as home:
            self._register(home)
            res = update_repo("acme/sample", merge_policy=" ASK ", max_fix_rounds=5)
            self.assertTrue(res["ok"])
            self.assertEqual(res["updated_fields"], ["max_fix_rounds", "merge_policy"])
            self.assertEqual(res["repo"]["merge_policy"], "ask")
            self.assertEqual(res["repo"]["max_fix_rounds"], 5)
            stored = self._store(home)["acme-sample"]
            self.assertEqual(stored["merge_policy"], "ask")
            self.assertEqual(stored["max_fix_rounds"], 5)
            self.assertEqual(stored["default_branch"], "main")

            res2 = update_repo("acme-sample", default_branch="  develop ", design_provider="Hermes", design_approval="auto")
            self.assertEqual(res2["repo"]["default_branch"], "develop")
            repo = get_repo("acme-sample")
            assert repo is not None
            self.assertEqual(repo["design_provider"], "hermes")
            self.assertEqual(repo["design_approval"], "auto")
            self.assertEqual(repo["merge_policy"], "ask")

    def test_update_repo_validation_is_strict_and_atomic(self):
        from factonio.repos import RepoNotFoundError, update_repo

        with isolated_hermes_home() as home:
            self._register(home)
            before = self._store(home)

            with self.assertRaises(RepoNotFoundError):
                update_repo("nope/missing", merge_policy="ask")
            self.assertTrue(issubclass(RepoNotFoundError, LookupError))
            with self.assertRaises(ValueError):
                update_repo("acme/sample")

            bad_cases = [
                {"merge_policy": None},
                {"merge_policy": ""},
                {"merge_policy": "always"},
                {"max_fix_rounds": True},
                {"max_fix_rounds": 2.0},
                {"max_fix_rounds": None},
                {"max_fix_rounds": "3"},
                {"max_fix_rounds": 0},
                {"max_fix_rounds": -1},
                {"default_branch": None},
                {"default_branch": "   "},
                {"default_branch": "-main"},
                {"default_branch": "a..b"},
                {"default_branch": "my branch"},
                {"default_branch": "ma\x01in"},
                {"design_provider": "claude"},
                {"design_provider": 3},
                {"design_approval": "never"},
                # Valid field + invalid field: nothing must be written.
                {"merge_policy": "ask", "max_fix_rounds": 0},
            ]
            for kwargs in bad_cases:
                with self.subTest(kwargs=kwargs):
                    with self.assertRaises(ValueError):
                        update_repo("acme/sample", **kwargs)
            self.assertEqual(self._store(home), before)

    def test_clearing_design_overrides_inherits_global_defaults(self):
        from factonio.config import resolve_design_provider, save_settings
        from factonio.repos import update_repo

        with isolated_hermes_home() as home:
            self._register(home)
            save_settings({"design_provider": "off", "design_approval": "auto"})
            update_repo("acme/sample", design_provider="hermes", design_approval="ask")
            d = resolve_design_provider(repo_slug="acme-sample")
            self.assertEqual((d["configured"], d["approval"]), ("hermes", "ask"))

            for token in (None, "", "inherit", "DEFAULT"):
                with self.subTest(token=token):
                    update_repo("acme/sample", design_provider="hermes", design_approval="ask")
                    update_repo("acme/sample", design_provider=token, design_approval=token)
                    stored = self._store(home)["acme-sample"]
                    self.assertNotIn("design_provider", stored)
                    self.assertNotIn("design_approval", stored)
                    d = resolve_design_provider(repo_slug="acme-sample")
                    self.assertEqual((d["configured"], d["approval"]), ("off", "auto"))

    def test_update_config_yaml_only_repo_migrates_all_fields(self):
        from factonio.config import get_repo, list_repos, resolve_design_provider, resolve_phase_model
        from factonio.repos import update_repo

        with isolated_hermes_home() as home:
            (home / "config.yaml").write_text(_CONFIG_REPO_YAML, encoding="utf-8")
            self.assertFalse((home / "plugin-data" / "factonio" / "repos.json").exists())
            original = list_repos()["acme-legacy"]

            res = update_repo("acme/legacy", max_fix_rounds=7, design_provider="inherit")
            self.assertEqual(res["updated_fields"], ["design_provider", "max_fix_rounds"])
            stored = self._store(home)["acme-legacy"]
            expected = {k: v for k, v in original.items() if k != "design_provider"}
            expected["max_fix_rounds"] = 7
            self.assertEqual(stored, expected)
            self.assertEqual(stored["required_checks"], ["ci", "lint"])
            self.assertEqual(stored["models"], {"implement": {"provider": "anthropic", "model": "claude-x"}})

            repo = get_repo("acme-legacy")
            assert repo is not None
            self.assertNotIn("design_provider", repo)
            d = resolve_design_provider(repo_slug="acme-legacy")
            self.assertEqual(d["configured"], "hermes")
            self.assertEqual(d["approval"], "auto")
            self.assertEqual(resolve_phase_model("implement", repo_slug="acme-legacy")["model"], "claude-x")
            # config.yaml itself is untouched.
            self.assertEqual((home / "config.yaml").read_text(encoding="utf-8"), _CONFIG_REPO_YAML)

    def test_list_repos_returns_deep_copies(self):
        from factonio.config import list_repos

        with isolated_hermes_home() as home:
            (home / "config.yaml").write_text(_CONFIG_REPO_YAML, encoding="utf-8")
            first = list_repos()
            first["acme-legacy"]["required_checks"].append("mutated")
            first["acme-legacy"]["models"]["implement"]["model"] = "mutated"
            second = list_repos()
            self.assertEqual(second["acme-legacy"]["required_checks"], ["ci", "lint"])
            self.assertEqual(second["acme-legacy"]["models"]["implement"]["model"], "claude-x")

    def test_add_repo_rerun_preserves_overrides(self):
        from factonio.config import list_repos, save_settings
        from factonio.repos import update_repo

        with isolated_hermes_home() as home:
            self._register(home)
            update_repo(
                "acme/sample",
                merge_policy="ask",
                max_fix_rounds=6,
                default_branch="develop",
                design_provider="off",
                design_approval="auto",
            )
            repos = list_repos()
            repos["acme-sample"]["ci_contract"] = "strict"
            repos["acme-sample"]["models"] = {"verify": "other-model"}
            save_settings({"repos": repos})

            res = self._register(home)
            r = res["repo"]
            self.assertEqual(r["merge_policy"], "ask")
            self.assertEqual(r["max_fix_rounds"], 6)
            self.assertEqual(r["default_branch"], "develop")
            self.assertEqual(r["design_provider"], "off")
            self.assertEqual(r["design_approval"], "auto")
            self.assertEqual(r["ci_contract"], "strict")
            self.assertEqual(r["models"], {"verify": "other-model"})

            res = self._register(
                home,
                merge_policy="auto",
                max_fix_rounds=2,
                default_branch="release",
                design_provider="inherit",
                design_approval="ask",
            )
            r = res["repo"]
            self.assertEqual((r["merge_policy"], r["max_fix_rounds"], r["default_branch"]), ("auto", 2, "release"))
            self.assertNotIn("design_provider", r)
            self.assertEqual(r["design_approval"], "ask")

    def test_add_repo_uses_shared_validators(self):
        with isolated_hermes_home() as home:
            for kwargs in (
                {"merge_policy": "always"},
                {"max_fix_rounds": 0},
                {"max_fix_rounds": True},
                {"default_branch": "bad branch"},
                {"design_provider": "nope"},
                {"design_approval": "never"},
            ):
                with self.subTest(kwargs=kwargs):
                    with self.assertRaises(ValueError):
                        self._register(home, **kwargs)
            self.assertFalse((home / "plugin-data" / "factonio" / "repos.json").exists())

            r = self._register(home, default_branch="develop", design_provider="hermes")["repo"]
            self.assertEqual(r["default_branch"], "develop")
            self.assertEqual(r["design_provider"], "hermes")
            self.assertNotIn("design_approval", r)
            self.assertEqual(r["merge_policy"], "auto")
            self.assertEqual(r["max_fix_rounds"], 3)


def _mp_update_worker(barrier, kwargs, queue):
    """Child-process entry: wait on the barrier, then call update_repo concurrently."""
    try:
        from factonio.repos import update_repo

        barrier.wait(timeout=30)
        res = update_repo("acme/sample", **kwargs)
        queue.put(("ok", bool(res.get("ok"))))
    except BaseException as exc:  # noqa: BLE001 - report every failure to the parent
        queue.put(("error", f"{type(exc).__name__}: {exc}"))


class TestRepoStoreConcurrency(unittest.TestCase):
    def _register(self, home: Path, spec: str):
        from factonio.repos import add_repo

        local = home / "dev" / spec.split("/")[1]
        local.mkdir(parents=True, exist_ok=True)
        (local / ".git").mkdir(exist_ok=True)
        return add_repo(spec, local_path=str(local), clone=False, inspect_remote=False)

    def _store_path(self, home: Path) -> Path:
        return home / "plugin-data" / "factonio" / "repos.json"

    def test_concurrent_thread_updates_preserve_all_changes(self):
        import threading

        from factonio import config as cfg
        from factonio.repos import update_repo

        with isolated_hermes_home() as home:
            self._register(home, "acme/sample")
            self._register(home, "acme/other")

            # Widen the race window: every read of the store yields to other threads.
            real_read = cfg._read_repos_store

            def slow_read():
                data = real_read()
                time.sleep(0.02)
                return data

            jobs = [
                ("acme/sample", {"merge_policy": "ask"}),
                ("acme/sample", {"max_fix_rounds": 7}),
                ("acme/other", {"default_branch": "develop"}),
                ("acme/other", {"design_provider": "hermes"}),
            ]
            barrier = threading.Barrier(len(jobs))
            results: list = [None] * len(jobs)

            def run(i, repo, kwargs):
                barrier.wait(timeout=10)
                try:
                    results[i] = update_repo(repo, **kwargs)
                except BaseException as exc:  # noqa: BLE001
                    results[i] = exc

            with mock.patch.object(cfg, "_read_repos_store", slow_read):
                threads = [threading.Thread(target=run, args=(i, r, k)) for i, (r, k) in enumerate(jobs)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=30)

            for res in results:
                self.assertIsInstance(res, dict, msg=repr(res))
                self.assertTrue(res["ok"])
            store = json.loads(self._store_path(home).read_text(encoding="utf-8"))
            self.assertEqual(store["acme-sample"]["merge_policy"], "ask")
            self.assertEqual(store["acme-sample"]["max_fix_rounds"], 7)
            self.assertEqual(store["acme-other"]["default_branch"], "develop")
            self.assertEqual(store["acme-other"]["design_provider"], "hermes")
            self.assertEqual(store["acme-other"]["merge_policy"], "auto")
            self.assertEqual(store["acme-other"]["max_fix_rounds"], 3)
            leftovers = [p.name for p in self._store_path(home).parent.glob("*.tmp")]
            self.assertEqual(leftovers, [])

    def test_concurrent_process_updates_never_corrupt_store(self):
        import multiprocessing

        from factonio.config import get_repo

        try:
            ctx = multiprocessing.get_context("fork")
        except ValueError:
            self.skipTest("fork start method unavailable on this platform")

        trials = 8
        for trial in range(trials):
            with self.subTest(trial=trial), isolated_hermes_home() as home:
                self._register(home, "acme/sample")
                barrier = ctx.Barrier(2)
                queue = ctx.Queue()
                procs = [
                    ctx.Process(target=_mp_update_worker, args=(barrier, {"merge_policy": "ask"}, queue)),
                    ctx.Process(target=_mp_update_worker, args=(barrier, {"max_fix_rounds": 9}, queue)),
                ]
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    for p in procs:
                        p.start()
                outcomes = [queue.get(timeout=60) for _ in procs]
                for p in procs:
                    p.join(timeout=60)
                    self.assertEqual(p.exitcode, 0)
                self.assertEqual(outcomes, [("ok", True), ("ok", True)], msg=repr(outcomes))

                raw = self._store_path(home).read_text(encoding="utf-8")
                store = json.loads(raw)  # must be well-formed JSON
                self.assertEqual(store["acme-sample"]["merge_policy"], "ask")
                self.assertEqual(store["acme-sample"]["max_fix_rounds"], 9)
                repo = get_repo("acme/sample")
                self.assertIsNotNone(repo)
                leftovers = [p.name for p in self._store_path(home).parent.glob("*.tmp")]
                self.assertEqual(leftovers, [])

    def test_save_repos_store_uses_unique_temp_and_cleans_up_on_failure(self):
        from factonio import config as cfg

        with isolated_hermes_home() as home:
            self._register(home, "acme/sample")
            before = self._store_path(home).read_text(encoding="utf-8")
            with mock.patch.object(cfg.os, "replace", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    cfg.save_repos_store({"x": {"repo": "a/b"}})
            self.assertEqual(self._store_path(home).read_text(encoding="utf-8"), before)
            self.assertEqual(list(self._store_path(home).parent.glob("*.tmp")), [])
            self.assertFalse((self._store_path(home).parent / "repos.json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
