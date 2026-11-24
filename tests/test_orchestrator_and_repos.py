"""Clean-room tests for repo registration, Kanban DAG creation, holdout barrier, and PR gate."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
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

        green_payload = {
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
            "statusCheckRollup": [{"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}],
        }
        red_payload = {
            **green_payload,
            "statusCheckRollup": [{"name": "ci", "status": "COMPLETED", "conclusion": "FAILURE"}],
        }

        def _proc(stdout: str, rc: int = 0):
            return subprocess.CompletedProcess(args=["gh"], returncode=rc, stdout=stdout, stderr="")

        with isolated_hermes_home():
            with mock.patch("factonio.repos._run_gh", return_value=_proc(json.dumps(red_payload))):
                gate = inspect_pr_gate("acme/sample", 12)
                self.assertTrue(gate["ok"])
                self.assertFalse(gate["ready_to_merge"])
                self.assertTrue(any("Failed checks" in b for b in gate["blockers"]))

            with mock.patch("factonio.repos._run_gh", return_value=_proc(json.dumps(green_payload))):
                stale = merge_pr("acme/sample", 12, expected_head_sha="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
                self.assertFalse(stale["merged"])
                self.assertIn("HEAD mismatch", stale["error"])

            with mock.patch("factonio.repos._run_gh", side_effect=[_proc(json.dumps(green_payload)), _proc("")]):
                merged = merge_pr("acme/sample", 12, expected_head_sha="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
                self.assertTrue(merged["ok"])
                self.assertTrue(merged["merged"])


if __name__ == "__main__":
    unittest.main()
