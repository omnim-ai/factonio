"""Clean-room tests for the GraphQL PR gate, merge readback/local sync, and git patch helpers."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest import mock

from tests._env import PLUGIN_ROOT, isolated_hermes_home

if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

HEAD_SHA = "a" * 40
LEGACY_KEYS = (
    "ok", "repo", "pr_number", "url", "title", "state", "is_draft", "head_branch", "head_sha",
    "base_branch", "mergeable", "merge_state_status", "checks", "checks_green", "ready_to_merge", "blockers",
)
NEW_KEYS = ("review_decision", "unresolved_threads", "blocker_tier", "head_rollup_state")


def _proc(stdout: str = "", rc: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["gh"], returncode=rc, stdout=stdout, stderr=stderr)


def _check(name: str, conclusion: Optional[str] = "SUCCESS", status: str = "COMPLETED") -> dict:
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion}


def _status(context: str, state: str = "SUCCESS") -> dict:
    return {"__typename": "StatusContext", "context": context, "state": state}


def _thread(tid: str, *, resolved: bool = False, body: str = "Please fix", line: Optional[int] = 10,
            original_line: Optional[int] = None, path: str = "src/app.py") -> dict:
    return {
        "id": tid,
        "isResolved": resolved,
        "path": path,
        "line": line,
        "originalLine": original_line,
        "comments": {"nodes": [{"author": {"login": "reviewer"}, "body": body, "createdAt": "2026-11-20T10:00:00Z"}]},
    }


def _page(nodes: list, cursor: Optional[str] = None) -> dict:
    return {"pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor}, "nodes": nodes}


def _pr(*, thread_pages: Optional[list[dict]] = None, context_pages: Optional[list[dict]] = None,
        rollup_state: Optional[str] = "SUCCESS", **fields: Any) -> dict:
    """Return a PR fixture: base fields + paged threads/contexts consumed by `FakeGh`."""
    pr = {
        "number": 12,
        "url": "https://github.com/acme/sample/pull/12",
        "title": "Add retries",
        "state": "OPEN",
        "isDraft": False,
        "headRefName": "feat/retries",
        "headRefOid": HEAD_SHA,
        "baseRefName": "main",
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "reviewDecision": "APPROVED",
    }
    pr.update(fields)
    return {
        "pr": pr,
        "thread_pages": thread_pages if thread_pages is not None else [_page([])],
        "context_pages": context_pages if context_pages is not None else [_page([_check("ci")])],
        "rollup_state": rollup_state,
    }


class FakeGh:
    """Stand-in for `factonio.repos._run_gh` that serves paginated GraphQL and merge/readback calls."""

    def __init__(self, fixture: dict, *, merge_rc: int = 0, readback: Any = None) -> None:
        self.fixture = fixture
        self.merge_rc = merge_rc
        self.readback = readback
        self.calls: list[list[str]] = []

    @staticmethod
    def _fields(args: list[str]) -> dict[str, str]:
        out = {}
        for i, a in enumerate(args):
            if a in ("-f", "-F") and i + 1 < len(args):
                k, _, v = args[i + 1].partition("=")
                out[k] = v
        return out

    def _pager(self, pages: list[dict], cursor: Optional[str]) -> dict:
        if not cursor:
            return pages[0]
        idx = int(cursor.rsplit("-", 1)[1])
        return pages[idx]

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        if args[:2] == ["api", "graphql"]:
            f = self._fields(args)
            assert f["owner"] == "acme" and f["name"] == "sample", f
            assert "reviewThreads" in f["query"] and "statusCheckRollup" in f["query"]
            fx = self.fixture
            pr = dict(fx["pr"])
            pr["reviewThreads"] = self._pager(fx["thread_pages"], f.get("threadsCursor"))
            rollup = None
            if fx["rollup_state"] is not None or fx["context_pages"]:
                rollup = {"state": fx["rollup_state"], "contexts": self._pager(fx["context_pages"], f.get("contextsCursor"))}
            pr["commits"] = {"nodes": [{"commit": {"oid": pr["headRefOid"], "statusCheckRollup": rollup}}]}
            return _proc(json.dumps({"data": {"repository": {"pullRequest": pr}}}))
        if args[:2] == ["pr", "merge"]:
            return _proc("", rc=self.merge_rc, stderr="merge failed" if self.merge_rc else "")
        if args[:2] == ["pr", "view"]:
            if isinstance(self.readback, subprocess.CompletedProcess):
                return self.readback
            return _proc(json.dumps(self.readback))
        raise AssertionError(f"unexpected gh call {args}")


def _gate(fixture: dict, **kwargs: Any) -> dict:
    from factonio.repos import inspect_pr_gate

    with mock.patch("factonio.repos._run_gh", FakeGh(fixture, **kwargs)):
        return inspect_pr_gate("acme/sample", 12)


class TestGraphqlPrGate(unittest.TestCase):
    def test_ready_pr_preserves_legacy_keys_and_adds_new_keys(self):
        with isolated_hermes_home():
            fake = FakeGh(_pr())
            from factonio.repos import inspect_pr_gate

            with mock.patch("factonio.repos._run_gh", fake):
                gate = inspect_pr_gate("acme/sample", 12)
            for key in (*LEGACY_KEYS, *NEW_KEYS):
                self.assertIn(key, gate)
            self.assertTrue(gate["ok"])
            self.assertTrue(gate["ready_to_merge"])
            self.assertTrue(gate["checks_green"])
            self.assertEqual(gate["blockers"], [])
            self.assertIsNone(gate["blocker_tier"])
            self.assertEqual(gate["review_decision"], "APPROVED")
            self.assertEqual(gate["unresolved_threads"], [])
            self.assertEqual(gate["head_rollup_state"], "SUCCESS")
            self.assertEqual(gate["head_sha"], HEAD_SHA)
            self.assertEqual(gate["checks"], {"passed": ["ci"], "failed": [], "pending": []})
            # Uses GraphQL, not `gh pr view`.
            self.assertEqual([c[:2] for c in fake.calls], [["api", "graphql"]])
            self.assertIn("number=12", fake.calls[0])

    def test_paginates_more_than_100_checks_and_status_contexts(self):
        page1 = [_check(f"job-{i}") for i in range(100)]
        page2 = [_check(f"job-{i}") for i in range(100, 160)] + [_status("legacy/ci"), _status("deploy", "ERROR")]
        page3 = [_check("slow", None, "IN_PROGRESS"), _status("coverage", "PENDING")]
        with isolated_hermes_home():
            fake = FakeGh(_pr(context_pages=[_page(page1, "ctx-1"), _page(page2, "ctx-2"), _page(page3)],
                              rollup_state="FAILURE"))
            from factonio.repos import inspect_pr_gate

            with mock.patch("factonio.repos._run_gh", fake):
                gate = inspect_pr_gate("acme/sample", 12)
            self.assertEqual(len(gate["checks"]["passed"]), 161)
            self.assertIn("legacy/ci", gate["checks"]["passed"])
            self.assertEqual(gate["checks"]["failed"], ["deploy (error)"])
            self.assertEqual(gate["checks"]["pending"], ["slow", "coverage"])
            self.assertFalse(gate["checks_green"])
            self.assertEqual(gate["head_rollup_state"], "FAILURE")
            self.assertEqual(gate["blocker_tier"], 3)
            cursors = [FakeGh._fields(c).get("contextsCursor") for c in fake.calls]
            self.assertEqual(cursors, [None, "ctx-1", "ctx-2"])

    def test_paginates_review_threads_and_normalizes_unresolved(self):
        long_body = "Line one\n\n   needs   work " + ("x" * 400)
        page1 = [_thread(f"T{i}", resolved=(i % 2 == 0)) for i in range(100)]
        page2 = [
            _thread("T-last", body=long_body, line=None, original_line=42, path="src/db.py"),
            _thread("T-ok", resolved=True),
        ]
        with isolated_hermes_home():
            fake = FakeGh(_pr(thread_pages=[_page(page1, "thr-1"), _page(page2)]))
            from factonio.repos import inspect_pr_gate

            with mock.patch("factonio.repos._run_gh", fake):
                gate = inspect_pr_gate("acme/sample", 12)
            threads = gate["unresolved_threads"]
            self.assertEqual(len(threads), 51)
            self.assertEqual(set(threads[0]), {"id", "path", "line", "author", "body", "created_at"})
            last = threads[-1]
            self.assertEqual(last["id"], "T-last")
            self.assertEqual(last["path"], "src/db.py")
            self.assertEqual(last["line"], 42)  # falls back to originalLine
            self.assertEqual(last["author"], "reviewer")
            self.assertEqual(last["created_at"], "2026-11-20T10:00:00Z")
            self.assertLessEqual(len(last["body"]), 200)
            self.assertTrue(last["body"].startswith("Line one needs work x"))
            self.assertTrue(last["body"].endswith("..."))
            self.assertEqual(threads[0]["body"], "Please fix")
            self.assertEqual(gate["blocker_tier"], 2)
            self.assertEqual(gate["blockers"], ["Unresolved review threads (51)"])
            self.assertFalse(gate["ready_to_merge"])
            cursors = [FakeGh._fields(c).get("threadsCursor") for c in fake.calls]
            self.assertEqual(cursors, [None, "thr-1"])

    def test_pagination_is_capped_at_20_pages(self):
        pages = [_page([_check(f"c{i}")], f"ctx-{i + 1}") for i in range(30)]
        with isolated_hermes_home():
            fake = FakeGh(_pr(context_pages=pages))
            from factonio.repos import inspect_pr_gate

            with mock.patch("factonio.repos._run_gh", fake):
                gate = inspect_pr_gate("acme/sample", 12)
            self.assertEqual(len(fake.calls), 20)
            self.assertEqual(len(gate["checks"]["passed"]), 20)
            self.assertFalse(gate["ready_to_merge"])  # incomplete check list never reads as green
            self.assertEqual(gate["blocker_tier"], 3)

    def test_each_blocker_tier_in_isolation(self):
        cases = {
            1: _pr(mergeable="CONFLICTING", mergeStateStatus="DIRTY"),
            2: _pr(thread_pages=[_page([_thread("T1")])]),
            3: _pr(context_pages=[_page([_check("ci", "TIMED_OUT")])], rollup_state="FAILURE"),
            4: _pr(isDraft=True),
        }
        expected = {
            1: ["PR has merge conflicts"],
            2: ["Unresolved review threads (1)"],
            3: ["Failed checks: ci (timed_out)"],
            4: ["PR is still a draft"],
        }
        with isolated_hermes_home():
            for tier, fx in cases.items():
                gate = _gate(fx)
                self.assertEqual(gate["blocker_tier"], tier)
                self.assertEqual(gate["blockers"], expected[tier])
                self.assertFalse(gate["ready_to_merge"])
            # Tier 1 via mergeStateStatus alone.
            self.assertEqual(_gate(_pr(mergeStateStatus="DIRTY"))["blocker_tier"], 1)
            # Tier 3 via pending checks.
            self.assertEqual(
                _gate(_pr(context_pages=[_page([_check("ci", None, "QUEUED")])], rollup_state="PENDING"))["blockers"],
                ["Pending checks: ci"],
            )
            # Tier 3: BLOCKED + head rollup ERROR even with no individual contexts.
            gate = _gate(_pr(mergeStateStatus="BLOCKED", context_pages=[_page([])], rollup_state="ERROR"))
            self.assertEqual(gate["blocker_tier"], 3)
            self.assertEqual(gate["blockers"], ["Merge blocked with head statusCheckRollup=ERROR"])
            self.assertFalse(gate["checks_green"])
            # BLOCKED with a green rollup (e.g. awaiting review) is not a tier-3 CI blocker.
            self.assertIsNone(_gate(_pr(mergeStateStatus="BLOCKED"))["blocker_tier"])
            # Tier 4 gates.
            self.assertEqual(_gate(_pr(state="MERGED"))["blockers"], ["PR state is MERGED"])
            self.assertEqual(
                _gate(_pr(reviewDecision="CHANGES_REQUESTED"))["blockers"],
                ["Review decision is CHANGES_REQUESTED"],
            )
            self.assertIsNone(_gate(_pr(reviewDecision=None))["review_decision"])

    def test_blockers_are_ordered_tier_1_to_4(self):
        fx = _pr(
            state="CLOSED",
            isDraft=True,
            reviewDecision="CHANGES_REQUESTED",
            mergeable="CONFLICTING",
            mergeStateStatus="BLOCKED",
            thread_pages=[_page([_thread("T1"), _thread("T2")])],
            context_pages=[_page([_check("lint", "FAILURE"), _check("test", None, "IN_PROGRESS")])],
            rollup_state="FAILURE",
        )
        with isolated_hermes_home():
            gate = _gate(fx)
        self.assertEqual(gate["blocker_tier"], 1)
        self.assertEqual(gate["blockers"], [
            "PR has merge conflicts",
            "Unresolved review threads (2)",
            "Failed checks: lint (failure)",
            "Pending checks: test",
            "Merge blocked with head statusCheckRollup=FAILURE",
            "PR state is CLOSED",
            "PR is still a draft",
            "Review decision is CHANGES_REQUESTED",
        ])
        # Without conflicts the highest-priority tier becomes 2, then 3, then 4.
        with isolated_hermes_home():
            fx["pr"]["mergeable"] = "MERGEABLE"
            self.assertEqual(_gate(fx)["blocker_tier"], 2)
            fx["thread_pages"] = [_page([])]
            self.assertEqual(_gate(fx)["blocker_tier"], 3)
            fx["context_pages"] = [_page([_check("ci")])]
            fx["rollup_state"] = "SUCCESS"
            gate = _gate(fx)
            self.assertEqual(gate["blocker_tier"], 4)
            self.assertTrue(gate["checks_green"])

    def test_errors_return_clean_failure(self):
        from factonio.repos import inspect_pr_gate

        responses = {
            "gh exit": _proc("", rc=1, stderr="HTTP 401: Bad credentials"),
            "invalid json": _proc("not json"),
            "graphql errors": _proc(json.dumps({"errors": [{"message": "Could not resolve to a Repository"}]})),
            "missing pr": _proc(json.dumps({"data": {"repository": {"pullRequest": None}}})),
            "missing repo": _proc(json.dumps({"data": {"repository": None}})),
        }
        with isolated_hermes_home():
            for label, resp in responses.items():
                with mock.patch("factonio.repos._run_gh", return_value=resp):
                    gate = inspect_pr_gate("acme/sample", "12")
                self.assertEqual(set(gate), {"ok", "repo", "pr_number", "error"}, label)
                self.assertFalse(gate["ok"])
                self.assertEqual(gate["repo"], "acme/sample")
                self.assertEqual(gate["pr_number"], 12)
                self.assertTrue(gate["error"], label)
            with mock.patch("factonio.repos._run_gh", return_value=responses["gh exit"]):
                self.assertIn("Bad credentials", inspect_pr_gate("acme/sample", 12)["error"])
            with mock.patch("factonio.repos._run_gh", return_value=responses["graphql errors"]):
                self.assertIn("Could not resolve", inspect_pr_gate("acme/sample", 12)["error"])

            # A failing follow-up page is an error, not a silently partial gate.
            fake = FakeGh(_pr(context_pages=[_page([_check("a")], "ctx-1"), _page([_check("b")])]))
            calls = {"n": 0}

            def flaky(args, **kw):
                calls["n"] += 1
                return fake(args, **kw) if calls["n"] == 1 else _proc("", rc=1, stderr="rate limited")

            with mock.patch("factonio.repos._run_gh", side_effect=flaky):
                gate = inspect_pr_gate("acme/sample", 12)
            self.assertFalse(gate["ok"])
            self.assertIn("rate limited", gate["error"])


class TestPrGateCli(unittest.TestCase):
    def _run(self, fixture: Any, *argv: str) -> tuple[int, str]:
        from factonio.commands import register_cli

        class _Ctx:
            cli: dict = {}

            def register_cli_command(self, name, *, help, setup_fn, handler_fn):
                self.cli[name] = (setup_fn, handler_fn)

        ctx = _Ctx()
        register_cli(ctx, PLUGIN_ROOT)
        setup_fn, handler_fn = ctx.cli["factonio"]
        parser = argparse.ArgumentParser(prog="hermes factonio")
        setup_fn(parser)
        out = io.StringIO()
        code = 0
        runner = fixture if isinstance(fixture, mock.Mock) else FakeGh(fixture)
        with mock.patch("factonio.repos._run_gh", runner), contextlib.redirect_stdout(out):
            try:
                handler_fn(parser.parse_args(list(argv)))
            except SystemExit as exc:
                code = int(exc.code or 0)
        return code, out.getvalue()

    def test_text_output_ready_blocked_and_error(self):
        with isolated_hermes_home():
            code, out = self._run(_pr(), "pr-gate", "acme/sample", "12")
            self.assertEqual(code, 0)
            self.assertIn("READY: acme/sample#12", out)
            self.assertIn("Review: decision=APPROVED unresolved_threads=0", out)

            blocked = _pr(reviewDecision=None, thread_pages=[_page([_thread("T1"), _thread("T2")])])
            code, out = self._run(blocked, "pr-gate", "acme/sample", "12")
            self.assertEqual(code, 1)
            self.assertIn("BLOCKED (tier 2): acme/sample#12", out)
            self.assertIn("Review: decision=NONE unresolved_threads=2", out)
            self.assertIn("Blocker: Unresolved review threads (2)", out)

            err = mock.Mock(return_value=_proc("", rc=1, stderr="no auth"))
            code, out = self._run(err, "pr-gate", "acme/sample", "12")
            self.assertEqual(code, 1)
            self.assertIn("Error: no auth", out)

    def test_json_output_preserved(self):
        with isolated_hermes_home():
            code, out = self._run(_pr(mergeable="CONFLICTING"), "--json", "pr-gate", "acme/sample", "12")
            self.assertEqual(code, 1)
            data = json.loads(out)
            self.assertEqual(data["blocker_tier"], 1)
            for key in (*LEGACY_KEYS, *NEW_KEYS):
                self.assertIn(key, data)
            code, out = self._run(_pr(), "--json", "pr-gate", "acme/sample", "12")
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(out)["ready_to_merge"])


# ---------------------------------------------------------------------------
# Real-git fixtures
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout.strip()


def _git_env() -> contextlib.AbstractContextManager:
    env = {
        "GIT_AUTHOR_NAME": "Factonio Test", "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Factonio Test", "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return mock.patch.dict(os.environ, env)


def _commit(repo: Path, files: dict[str, str], msg: str) -> str:
    for name, content in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


def _make_origin(home: Path) -> tuple[Path, Path, Path]:
    """Bare origin + operator clone (registered) + upstream clone (simulates GitHub merges)."""
    origin = home / "origin.git"
    _git(home, "init", "-q", "--bare", "-b", "main", str(origin))
    seed = home / "seed"
    _git(home, "clone", "-q", str(origin), str(seed))
    _git(seed, "symbolic-ref", "HEAD", "refs/heads/main")
    _commit(seed, {"a.txt": "a\n", "b.txt": "b\n"}, "init")
    _git(seed, "push", "-q", "origin", "main")
    local = home / "dev" / "sample"
    local.parent.mkdir(parents=True, exist_ok=True)
    _git(home, "clone", "-q", str(origin), str(local))
    return origin, local, seed


@unittest.skipUnless(shutil.which("git"), "git is required")
class TestMergeReadbackAndLocalSync(unittest.TestCase):
    def _merge(self, readback: Any, *, local: Optional[Path] = None, merge_rc: int = 0) -> tuple[dict, FakeGh]:
        from factonio.repos import add_repo, merge_pr

        if local is not None:
            add_repo("acme/sample", local_path=str(local), clone=False, inspect_remote=False)
        fake = FakeGh(_pr(), merge_rc=merge_rc, readback=readback)
        with mock.patch("factonio.repos._run_gh", fake):
            return merge_pr("acme/sample", 12, expected_head_sha=HEAD_SHA), fake

    def test_queued_closed_and_readback_failures(self):
        with isolated_hermes_home():
            res, fake = self._merge({"state": "OPEN", "mergedAt": None, "baseRefName": "main"})
            self.assertTrue(res["ok"])
            self.assertFalse(res["merged"])
            self.assertTrue(res["queued"])
            self.assertEqual(res["state"], "OPEN")
            self.assertFalse(res["local_synced"])
            self.assertIn("queued", res["local_sync_note"])
            for key in ("repo", "pr_number", "url", "head_sha", "method"):
                self.assertIn(key, res)
            view = [c for c in fake.calls if c[:2] == ["pr", "view"]][0]
            self.assertEqual(view[view.index("--json") + 1], "state,mergedAt,baseRefName")

            res, _ = self._merge({"state": "CLOSED", "mergedAt": None, "baseRefName": "main"})
            self.assertFalse(res["ok"])
            self.assertFalse(res["merged"])
            self.assertFalse(res["queued"])
            self.assertIn("CLOSED", res["error"])

            res, _ = self._merge(_proc("", rc=1, stderr="network down"))
            self.assertFalse(res["ok"])
            self.assertFalse(res["merged"])
            self.assertIn("network down", res["error"])

            res, _ = self._merge(_proc("{oops"))
            self.assertFalse(res["ok"])
            self.assertFalse(res["merged"])

            res, fake = self._merge(None, merge_rc=1)
            self.assertFalse(res["ok"])
            self.assertFalse(res["merged"])
            self.assertFalse(any(c[:2] == ["pr", "view"] for c in fake.calls))

    def test_merged_without_registered_checkout_does_not_fail(self):
        with isolated_hermes_home():
            res, _ = self._merge({"state": "MERGED", "mergedAt": "2026-11-25T12:00:00Z", "baseRefName": "main"})
            self.assertTrue(res["ok"])
            self.assertTrue(res["merged"])
            self.assertFalse(res["queued"])
            self.assertEqual(res["merged_at"], "2026-11-25T12:00:00Z")
            self.assertFalse(res["local_synced"])
            self.assertIn("skipped", res["local_sync_note"])

    def test_merged_fast_forwards_clean_checked_out_default_branch(self):
        with isolated_hermes_home() as home, _git_env():
            _, local, seed = _make_origin(home)
            new_sha = _commit(seed, {"a.txt": "merged\n"}, "squash merge")
            _git(seed, "push", "-q", "origin", "main")
            res, _ = self._merge(
                {"state": "MERGED", "mergedAt": "2026-11-25T12:00:00Z", "baseRefName": "main"}, local=local
            )
            self.assertTrue(res["ok"] and res["merged"])
            self.assertTrue(res["local_synced"], res["local_sync_note"])
            self.assertEqual(_git(local, "rev-parse", "main"), new_sha)
            self.assertEqual((local / "a.txt").read_text(), "merged\n")

    def test_merged_skips_dirty_checked_out_default_branch(self):
        with isolated_hermes_home() as home, _git_env():
            _, local, seed = _make_origin(home)
            before = _git(local, "rev-parse", "main")
            _commit(seed, {"a.txt": "merged\n"}, "squash merge")
            _git(seed, "push", "-q", "origin", "main")
            (local / "a.txt").write_text("local edit\n")
            res, _ = self._merge(
                {"state": "MERGED", "mergedAt": "2026-11-25T12:00:00Z", "baseRefName": "main"}, local=local
            )
            self.assertTrue(res["ok"] and res["merged"])
            self.assertFalse(res["local_synced"])
            self.assertIn("dirty", res["local_sync_note"])
            self.assertEqual(_git(local, "rev-parse", "main"), before)
            self.assertEqual((local / "a.txt").read_text(), "local edit\n")

    def test_merged_updates_default_branch_ref_while_other_branch_checked_out(self):
        with isolated_hermes_home() as home, _git_env():
            _, local, seed = _make_origin(home)
            _git(local, "checkout", "-q", "-b", "feature")
            feature_sha = _commit(local, {"c.txt": "feature\n"}, "feature work")
            (local / "c.txt").write_text("uncommitted\n")
            new_sha = _commit(seed, {"a.txt": "merged\n"}, "squash merge")
            _git(seed, "push", "-q", "origin", "main")
            res, _ = self._merge(
                {"state": "MERGED", "mergedAt": "2026-11-25T12:00:00Z", "baseRefName": "main"}, local=local
            )
            self.assertTrue(res["ok"] and res["merged"])
            self.assertTrue(res["local_synced"], res["local_sync_note"])
            self.assertEqual(_git(local, "rev-parse", "main"), new_sha)
            self.assertEqual(_git(local, "rev-parse", "--abbrev-ref", "HEAD"), "feature")
            self.assertEqual(_git(local, "rev-parse", "HEAD"), feature_sha)
            self.assertEqual((local / "c.txt").read_text(), "uncommitted\n")
            self.assertEqual((local / "a.txt").read_text(), "a\n")

    def test_merged_never_touches_default_branch_checked_out_in_other_worktree(self):
        with isolated_hermes_home() as home, _git_env():
            _, local, seed = _make_origin(home)
            _git(local, "checkout", "-q", "-b", "feature")
            wt = home / "wt-main"
            _git(local, "worktree", "add", "-q", str(wt), "main")
            before = _git(local, "rev-parse", "main")
            _commit(seed, {"a.txt": "merged\n"}, "squash merge")
            _git(seed, "push", "-q", "origin", "main")
            res, _ = self._merge(
                {"state": "MERGED", "mergedAt": "2026-11-25T12:00:00Z", "baseRefName": "main"}, local=local
            )
            self.assertTrue(res["ok"] and res["merged"])
            self.assertFalse(res["local_synced"])
            self.assertTrue(res["local_sync_note"].startswith("skipped"))
            self.assertEqual(_git(local, "rev-parse", "main"), before)
            self.assertEqual((wt / "a.txt").read_text(), "a\n")

    def test_diverged_default_branch_is_never_rewritten(self):
        with isolated_hermes_home() as home, _git_env():
            _, local, seed = _make_origin(home)
            local_only = _commit(local, {"b.txt": "local only\n"}, "local commit")
            _commit(seed, {"a.txt": "merged\n"}, "squash merge")
            _git(seed, "push", "-q", "origin", "main")
            res, _ = self._merge(
                {"state": "MERGED", "mergedAt": "2026-11-25T12:00:00Z", "baseRefName": "main"}, local=local
            )
            self.assertTrue(res["ok"] and res["merged"])
            self.assertFalse(res["local_synced"])
            self.assertEqual(_git(local, "rev-parse", "main"), local_only)


@unittest.skipUnless(shutil.which("git"), "git is required")
class TestPatchIdAndTrunkDrift(unittest.TestCase):
    def _feature_repo(self, home: Path) -> tuple[Path, Path, str]:
        _, local, seed = _make_origin(home)
        _git(local, "checkout", "-q", "-b", "feature")
        _commit(local, {"a.txt": "a\nfeature 1\n"}, "feature 1")
        head = _commit(local, {"c.txt": "new file\n"}, "feature 2")
        return local, seed, head

    def test_compute_patch_id_modes(self):
        from factonio.repos import compute_patch_id

        with isolated_hermes_home() as home, _git_env():
            local, _, head = self._feature_repo(home)
            single = compute_patch_id(local, head)
            self.assertRegex(single, r"^[0-9a-f]{40}$")
            whole = compute_patch_id(local, head, base_ref="main")
            self.assertRegex(whole, r"^[0-9a-f]{40}$")
            self.assertNotEqual(single, whole)
            self.assertEqual(compute_patch_id(local, "main..feature"), whole)
            # Same content → same id as the raw `git patch-id --stable` pipeline.
            diff = _git(local, "diff", "main...feature") + "\n"
            raw = subprocess.run(["git", "patch-id", "--stable"], cwd=local, input=diff, capture_output=True,
                                 text=True, check=True).stdout.split()[0]
            self.assertEqual(whole, raw)
            # Empty diff → None.
            self.assertIsNone(compute_patch_id(local, "main", base_ref="main"))
            with self.assertRaises(ValueError):
                compute_patch_id(local, "--output=/tmp/x")
            with self.assertRaises(ValueError):
                compute_patch_id(local, "main feature")
            with self.assertRaises(ValueError):
                compute_patch_id(local, head, base_ref="-p")
            with self.assertRaises(RuntimeError):
                compute_patch_id(local, "does-not-exist")

    def test_verify_patch_continuity_across_mechanical_rebase(self):
        from factonio.repos import verify_patch_continuity

        with isolated_hermes_home() as home, _git_env():
            local, seed, verified = self._feature_repo(home)
            same = verify_patch_continuity(local, verified, verified, base_ref="main")
            self.assertTrue(same["ok"] and same["continuous"] and same["same_commit"])

            # Trunk advances on an unrelated file; mechanical rebase keeps the patch-id.
            _commit(seed, {"b.txt": "trunk\n"}, "trunk change")
            _git(seed, "push", "-q", "origin", "main")
            _git(local, "fetch", "-q", "origin")
            _git(local, "rebase", "-q", "origin/main")
            rebased = _git(local, "rev-parse", "HEAD")
            self.assertNotEqual(rebased, verified)
            res = verify_patch_continuity(local, verified, rebased, base_ref="origin/main")
            self.assertTrue(res["ok"])
            self.assertTrue(res["continuous"], res)
            self.assertFalse(res["same_commit"])
            self.assertEqual(res["verified_patch_id"], res["head_patch_id"])
            self.assertEqual(res["verified_sha"], verified)
            self.assertEqual(res["head_sha"], rebased)
            self.assertIn("patch-id", res["reason"])

            # A content change after verification breaks continuity.
            changed = _commit(local, {"c.txt": "new file\nsneaky\n"}, "post-verify change")
            res = verify_patch_continuity(local, verified, changed, base_ref="origin/main")
            self.assertTrue(res["ok"])
            self.assertFalse(res["continuous"])
            self.assertNotEqual(res["verified_patch_id"], res["head_patch_id"])

            # Separate bases for the verified and live commits.
            res = verify_patch_continuity(local, verified, rebased, base_ref="origin/main", verified_base_ref="main")
            self.assertTrue(res["continuous"])

            bad = verify_patch_continuity(local, verified, "f" * 40, base_ref="main")
            self.assertFalse(bad["ok"])
            self.assertFalse(bad["continuous"])
            self.assertIn("error", bad)
            self.assertFalse(verify_patch_continuity(local, "-x", verified)["ok"])

    def test_assess_trunk_drift(self):
        from factonio.repos import assess_trunk_drift

        with isolated_hermes_home() as home, _git_env():
            local, seed, _ = self._feature_repo(home)
            calm = assess_trunk_drift(local, "feature")
            self.assertTrue(calm["ok"])
            self.assertEqual(calm["trunk_ref"], "origin/main")
            self.assertEqual(calm["trunk_commits_ahead"], 0)
            self.assertFalse(calm["has_drift"])
            self.assertFalse(calm["has_overlap"])
            self.assertEqual(calm["pr_files"], ["a.txt", "c.txt"])
            self.assertEqual(calm["trunk_files"], [])

            _commit(seed, {"a.txt": "a\ntrunk\n", "d.txt": "d\n"}, "trunk 1")
            _commit(seed, {"e.txt": "e\n"}, "trunk 2")
            _git(seed, "push", "-q", "origin", "main")
            stale = assess_trunk_drift(local, "feature")  # no fetch: still sees old origin/main
            self.assertEqual(stale["trunk_commits_ahead"], 0)

            drift = assess_trunk_drift(local, "feature", fetch=True)
            self.assertTrue(drift["ok"])
            self.assertEqual(drift["trunk_commits_ahead"], 2)
            self.assertTrue(drift["has_drift"])
            self.assertEqual(drift["trunk_files"], ["a.txt", "d.txt", "e.txt"])
            self.assertEqual(drift["overlapping_files"], ["a.txt"])
            self.assertTrue(drift["has_overlap"])
            self.assertRegex(drift["merge_base"], r"^[0-9a-f]{40}$")
            self.assertEqual(drift["head_ref"], "feature")

            for kwargs in ({"default_branch": "-bad"}, {"remote": "no/such"}, {"remote": "nope"}):
                bad = assess_trunk_drift(local, "feature", **kwargs)
                self.assertFalse(bad["ok"])
                self.assertEqual(
                    {k: bad[k] for k in ("has_drift", "has_overlap", "overlapping_files")},
                    {"has_drift": False, "has_overlap": False, "overlapping_files": []},
                )
                self.assertTrue(bad["error"])
            self.assertFalse(assess_trunk_drift(local, "--evil")["ok"])


if __name__ == "__main__":
    unittest.main()
