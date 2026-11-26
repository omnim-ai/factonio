"""Clean-room tests for Wave 4 PR gate hardening: approval gates, review automation, flake signal,
GraphQL retry, review-bot thread metadata, tiered trunk drift, patch continuity and the stack gate."""

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


def _proc(stdout: str = "", rc: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["gh"], returncode=rc, stdout=stdout, stderr=stderr)


def _check(name: str, conclusion: Optional[str] = "SUCCESS", status: str = "COMPLETED") -> dict:
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion}


def _status(context: str, state: str = "SUCCESS") -> dict:
    return {"__typename": "StatusContext", "context": context, "state": state}


def _conn(nodes: list) -> dict:
    return {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}


def _pr_node(
    number: int = 12,
    *,
    head_branch: str = "feat/x",
    head_sha: str = HEAD_SHA,
    base_branch: str = "main",
    contexts: Optional[list] = None,
    rollup_state: Optional[str] = "SUCCESS",
    threads: Optional[list] = None,
    history: Optional[list] = None,
    **fields: Any,
) -> dict:
    node = {
        "number": number,
        "url": f"https://github.com/acme/sample/pull/{number}",
        "title": f"PR {number}",
        "state": "OPEN",
        "isDraft": False,
        "headRefName": head_branch,
        "headRefOid": head_sha,
        "baseRefName": base_branch,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "reviewDecision": "APPROVED",
        "reviewThreads": _conn(threads or []),
        "commits": {"nodes": [{"commit": {"oid": head_sha, "statusCheckRollup": {
            "state": rollup_state,
            "contexts": _conn(contexts if contexts is not None else [_check("ci")]),
        }}}]},
    }
    if history is not None:
        node["commitHistory"] = {"nodes": [
            {"commit": {"oid": oid, "statusCheckRollup": ({"state": st} if st else None)}} for oid, st in history
        ]}
    node.update(fields)
    return node


class Gh:
    """Fake `gh`: GraphQL gate per PR number (or a scripted response list), merge and readback."""

    def __init__(self, prs: Optional[dict[int, dict]] = None, *, script: Optional[list] = None,
                 views: Optional[dict[int, dict]] = None) -> None:
        self.prs = prs or {}
        self.script = list(script or [])
        self.views = views or {}
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        if args[:2] == ["api", "graphql"]:
            if self.script:
                item = self.script.pop(0)
                if isinstance(item, BaseException):
                    raise item
                return item
            number = next(int(a.split("=", 1)[1]) for a in args if a.startswith("number="))
            if number not in self.prs:
                return _proc(json.dumps({"data": {"repository": {"pullRequest": None}}}))
            repo = {"pullRequest": self.prs[number], "defaultBranchRef": {"name": "main"}}
            return _proc(json.dumps({"data": {"repository": repo}}))
        if args[:2] == ["pr", "merge"]:
            return _proc("")
        if args[:2] == ["pr", "view"]:
            return _proc(json.dumps(self.views[int(args[2])]))
        raise AssertionError(f"unexpected gh call {args}")

    def graphql_calls(self) -> int:
        return sum(1 for c in self.calls if c[:2] == ["api", "graphql"])


def _gate(node: dict, **kwargs: Any) -> dict:
    from factonio.repos import inspect_pr_gate

    with mock.patch("factonio.repos._run_gh", Gh({int(node["number"]): node})):
        return inspect_pr_gate("acme/sample", int(node["number"]), **kwargs)


class TestApprovalGatesAndAutomation(unittest.TestCase):
    def test_action_required_and_code_review_gate_are_tier_4_not_ci(self):
        node = _pr_node(
            contexts=[_check("ci"), _check("Code Review Gate", "FAILURE"), _check("deploy-approval", "ACTION_REQUIRED")],
            rollup_state="FAILURE",
            mergeStateStatus="BLOCKED",
        )
        with isolated_hermes_home():
            gate = _gate(node)
        self.assertEqual(set(gate["checks"]), {"passed", "failed", "pending"})
        self.assertEqual(gate["checks"], {"passed": ["ci"], "failed": [], "pending": []})
        self.assertEqual(gate["approval_gates"], ["Code Review Gate", "deploy-approval"])
        self.assertTrue(gate["checks_green"])
        self.assertEqual(gate["blocker_tier"], 4)
        self.assertEqual(gate["blockers"], ["Approval gate required: Code Review Gate, deploy-approval"])
        self.assertEqual(gate["blocker_tiers"], {"1": [], "2": [], "3": [], "4": gate["blockers"]})
        self.assertFalse(gate["ready_to_merge"])

    def test_passing_approval_check_stays_passed_and_real_ci_failure_stays_tier_3(self):
        with isolated_hermes_home():
            ok = _gate(_pr_node(contexts=[_check("ci"), _check("Owner Approval"), _status("Manual Approval")]))
            self.assertEqual(ok["checks"]["passed"], ["ci", "Owner Approval", "Manual Approval"])
            self.assertEqual(ok["approval_gates"], [])
            self.assertTrue(ok["ready_to_merge"])

            mixed = _gate(_pr_node(
                contexts=[_check("ci", "FAILURE"), _check("code review gate", "ACTION_REQUIRED")],
                rollup_state="FAILURE",
                mergeStateStatus="BLOCKED",
            ))
            self.assertEqual(mixed["checks"]["failed"], ["ci (failure)"])
            self.assertEqual(mixed["approval_gates"], ["code review gate"])
            self.assertFalse(mixed["checks_green"])
            self.assertEqual(mixed["blocker_tier"], 3)
            self.assertIn("Merge blocked with head statusCheckRollup=FAILURE", mixed["blockers"])
            self.assertIn("Approval gate required: code review gate", mixed["blockers"])

            # Empty contexts + BLOCKED + ERROR rollup is still a Tier 3 CI blocker.
            empty = _gate(_pr_node(contexts=[], rollup_state="ERROR", mergeStateStatus="BLOCKED"))
            self.assertEqual(empty["blocker_tier"], 3)
            self.assertFalse(empty["checks_green"])

    def test_pending_review_automation_detected(self):
        with isolated_hermes_home():
            gate = _gate(_pr_node(
                contexts=[_check("ci"), _check("Cursor Bugbot", None, "IN_PROGRESS"), _status("Security Review", "PENDING")],
                rollup_state="PENDING",
            ))
            self.assertTrue(gate["review_automation_running"])
            self.assertEqual(gate["pending_review_automations"], ["Cursor Bugbot", "Security Review"])
            self.assertEqual(gate["checks"]["pending"], ["Cursor Bugbot", "Security Review"])
            self.assertEqual(gate["blocker_tier"], 3)

            done = _gate(_pr_node(contexts=[_check("ci"), _check("Cursor Bugbot"), _check("PR Review Automation", "NEUTRAL")]))
            self.assertFalse(done["review_automation_running"])
            self.assertEqual(done["pending_review_automations"], [])
            self.assertTrue(done["ready_to_merge"])


class TestFlakeSignalAndThreads(unittest.TestCase):
    def test_had_previous_passing_ci(self):
        with isolated_hermes_home():
            flaky = _gate(_pr_node(
                contexts=[_check("ci", "FAILURE")], rollup_state="FAILURE",
                history=[("b" * 40, "SUCCESS"), ("c" * 40, None), (HEAD_SHA, "FAILURE")],
            ))
            self.assertTrue(flaky["had_previous_passing_ci"])
            self.assertEqual(flaky["blocker_tier"], 3)
            # Only the head itself passed: not a flake signal.
            head_only = _gate(_pr_node(history=[("b" * 40, "FAILURE"), (HEAD_SHA, "SUCCESS")]))
            self.assertFalse(head_only["had_previous_passing_ci"])
            # No history at all (older fixtures) is False, not an error.
            self.assertFalse(_gate(_pr_node())["had_previous_passing_ci"])

        from factonio.repos import _PR_GATE_QUERY

        self.assertIn("commitHistory: commits(last: 50)", _PR_GATE_QUERY)
        self.assertIn("commits(last: 1)", _PR_GATE_QUERY)
        self.assertIn("databaseId author { login __typename }", _PR_GATE_QUERY)

    def test_thread_comment_id_and_review_bot(self):
        def thread(tid: str, author: Optional[dict], db_id: Any = 101) -> dict:
            return {"id": tid, "isResolved": False, "path": "a.py", "line": 3, "originalLine": None,
                    "comments": {"nodes": [{"databaseId": db_id, "author": author, "body": "x",
                                            "createdAt": "2026-11-20T10:00:00Z"}]}}

        threads = [
            thread("T1", {"login": "coderabbitai", "__typename": "Bot"}),
            thread("T2", {"login": "cursor[bot]", "__typename": "User"}, 202),
            thread("T3", {"login": "Copilot", "__typename": "User"}),
            thread("T4", {"login": "alice", "__typename": "User"}, None),
            thread("T5", None, "bad"),
            thread("T6", {"login": "acme-bot", "__typename": "User"}),
        ]
        with isolated_hermes_home():
            gate = _gate(_pr_node(threads=threads))
        got = {t["id"]: (t["comment_id"], t["is_review_bot"]) for t in gate["unresolved_threads"]}
        self.assertEqual(got, {
            "T1": (101, True), "T2": (202, True), "T3": (101, True),
            "T4": (None, False), "T5": (None, False), "T6": (101, True),
        })


class TestGraphqlRetry(unittest.TestCase):
    def _run(self, script: list, **kwargs: Any) -> tuple[Any, list[float], Gh]:
        from factonio.repos import _gate_graphql, _GateQueryError

        sleeps: list[float] = []
        gh = Gh(script=script)
        with mock.patch("factonio.repos._run_gh", gh):
            try:
                out: Any = _gate_graphql("acme/sample", 12, sleep_fn=sleeps.append, **kwargs)
            except _GateQueryError as exc:
                out = exc
        return out, sleeps, gh

    def _ok(self) -> subprocess.CompletedProcess:
        return _proc(json.dumps({"data": {"repository": {"pullRequest": _pr_node()}}}))

    def test_retryable_failures_recover_with_backoff(self):
        retryable = [
            _proc("", rc=1, stderr="HTTP 502: Bad Gateway"),
            _proc("", rc=1, stderr="API rate limit exceeded for user (HTTP 403)"),
            _proc("", rc=1, stderr="HTTP 429: Too Many Requests"),
            _proc("", rc=1, stderr="Post https://api.github.com/graphql: dial tcp: i/o timeout"),
            _proc('{"data": {"repo'),
            subprocess.TimeoutExpired(["gh"], 30),
            _proc(json.dumps({"errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]})),
        ]
        with isolated_hermes_home():
            for failure in retryable:
                out, sleeps, gh = self._run([failure, self._ok()])
                self.assertIsInstance(out, dict, failure)
                self.assertEqual(out["number"], 12)
                self.assertEqual(sleeps, [0.25], failure)
                self.assertEqual(gh.graphql_calls(), 2)

    def test_exhausted_retries_and_backoff_cap(self):
        from factonio.repos import _GateQueryError

        with isolated_hermes_home():
            out, sleeps, gh = self._run([_proc("", rc=1, stderr="HTTP 503")] * 5)
            self.assertIsInstance(out, _GateQueryError)
            self.assertIn("HTTP 503", str(out))
            self.assertEqual(gh.graphql_calls(), 3)
            self.assertEqual(sleeps, [0.25, 0.5])

            out, sleeps, gh = self._run([_proc("", rc=1, stderr="connection reset by peer")] * 8, max_attempts=7)
            self.assertEqual(gh.graphql_calls(), 7)
            self.assertEqual(sleeps, [0.25, 0.5, 1.0, 2.0, 4.0, 4.0])

    def test_non_retryable_failures_fail_fast(self):
        from factonio.repos import _GateQueryError

        fatal = [
            _proc("", rc=1, stderr="HTTP 401: Bad credentials"),
            _proc("", rc=1, stderr="HTTP 403: Resource not accessible by integration"),
            _proc("", rc=1, stderr="To get started with GitHub CLI, please run: gh auth login"),
            _proc(json.dumps({"errors": [{"type": "NOT_FOUND", "message": "Could not resolve to a Repository"}]})),
            _proc(json.dumps({"errors": [{"message": "Field 'bogus' doesn't exist on type 'PullRequest'"}]})),
            _proc(json.dumps({"data": {"repository": {"pullRequest": None}}})),
        ]
        with isolated_hermes_home():
            for failure in fatal:
                out, sleeps, gh = self._run([failure, self._ok()])
                self.assertIsInstance(out, _GateQueryError, failure)
                self.assertEqual(gh.graphql_calls(), 1, failure)
                self.assertEqual(sleeps, [], failure)

    def test_inspect_pr_gate_uses_patchable_sleep_hook(self):
        from factonio.repos import inspect_pr_gate

        sleeps: list[float] = []
        gh = Gh(script=[_proc("", rc=1, stderr="HTTP 502"), self._ok()])
        with isolated_hermes_home(), mock.patch("factonio.repos._run_gh", gh), \
                mock.patch("factonio.repos._graphql_sleep", sleeps.append):
            gate = inspect_pr_gate("acme/sample", 12)
        self.assertTrue(gate["ok"])
        self.assertTrue(gate["ready_to_merge"])
        self.assertEqual(sleeps, [0.25])


# ---------------------------------------------------------------------------
# Real-git fixtures
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)
    if proc.returncode != 0:
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


class Repo:
    """Bare origin + upstream `seed` clone + registered operator clone with a pushed `feat/x` branch."""

    def __init__(self, home: Path) -> None:
        self.origin = home / "origin.git"
        _git(home, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.seed = home / "seed"
        _git(home, "clone", "-q", str(self.origin), str(self.seed))
        _git(self.seed, "symbolic-ref", "HEAD", "refs/heads/main")
        _commit(self.seed, {"a.txt": "a\n", "b.txt": "b\n", ".github/workflows/ci.yml": "on: push\n"}, "init")
        _git(self.seed, "push", "-q", "origin", "main")
        _git(self.seed, "checkout", "-q", "-b", "feat/x")
        self.head = _commit(self.seed, {"a.txt": "a\nfeature\n"}, "feature")
        _git(self.seed, "push", "-q", "origin", "feat/x")
        _git(self.seed, "checkout", "-q", "main")
        self.local = home / "dev" / "sample"
        self.local.parent.mkdir(parents=True, exist_ok=True)
        _git(home, "clone", "-q", str(self.origin), str(self.local))

    def trunk(self, files: dict[str, str]) -> None:
        _git(self.seed, "checkout", "-q", "main")
        _commit(self.seed, files, "trunk")
        _git(self.seed, "push", "-q", "origin", "main")

    def register(self) -> None:
        from factonio.repos import add_repo

        add_repo("acme/sample", local_path=str(self.local), clone=False, inspect_remote=False)


@unittest.skipUnless(shutil.which("git"), "git is required")
class TestTieredTrunkDrift(unittest.TestCase):
    def test_policies(self):
        from factonio.repos import assess_trunk_drift

        cases = [
            ({}, "clean", False),
            ({"c.txt": "c\n"}, "disjoint", False),
            ({"uv.lock": "x\n", "web/package-lock.json": "{}\n"}, "lockfile_only", False),
            ({"a.txt": "trunk\n"}, "overlap_rebase_required", True),
            ({".github/workflows/ci.yml": "on: pull_request\n"}, "workflow_rebase_required", True),
        ]
        for files, policy, rebase in cases:
            with self.subTest(policy=policy), isolated_hermes_home() as home, _git_env():
                repo = Repo(home)
                if files:
                    repo.trunk(files)
                res = assess_trunk_drift(repo.local, "origin/feat/x", fetch=True)
                self.assertTrue(res["ok"], res)
                self.assertEqual((res["policy"], res["rebase_required"]), (policy, rebase))
                expected_wf = [".github/workflows/ci.yml"] if policy == "workflow_rebase_required" else []
                self.assertEqual(res["workflow_files"], expected_wf)
        with isolated_hermes_home() as home, _git_env():
            bad = assess_trunk_drift(Repo(home).local, "feat/x", remote="nope")
            self.assertEqual(
                (bad["ok"], bad["policy"], bad["rebase_required"], bad["workflow_files"]),
                (False, "unavailable", False, []),
            )

    def test_gate_wires_drift_and_patch_continuity(self):
        from factonio.repos import inspect_pr_gate

        with isolated_hermes_home() as home, _git_env():
            repo = Repo(home)
            node = _pr_node(head_branch="feat/x", head_sha=repo.head)
            gh = Gh({12: node})
            # Unregistered: no local checkout -> trunk_drift None, never an error.
            with mock.patch("factonio.repos._run_gh", gh):
                gate = inspect_pr_gate("acme/sample", 12)
            self.assertTrue(gate["ok"])
            self.assertIsNone(gate["trunk_drift"])
            self.assertIsNone(gate["patch_continuity"])

            repo.register()
            repo.trunk({"a.txt": "trunk\n"})
            with mock.patch("factonio.repos._run_gh", gh):
                stale = inspect_pr_gate("acme/sample", 12)  # fetch=False: local origin/main has not moved
                drifted = inspect_pr_gate("acme/sample", 12, fetch=True)
                skipped = inspect_pr_gate("acme/sample", 12, check_trunk_drift=False)
            self.assertEqual(stale["trunk_drift"]["policy"], "clean")
            self.assertTrue(stale["ready_to_merge"])
            self.assertEqual(drifted["trunk_drift"]["policy"], "overlap_rebase_required")
            self.assertEqual(drifted["blocker_tier"], 1)
            self.assertEqual(drifted["blockers"], ["Trunk drift requires rebase (overlap_rebase_required): a.txt"])
            self.assertFalse(drifted["ready_to_merge"])
            self.assertIsNone(skipped["trunk_drift"])

            # Unresolvable head (e.g. mock SHA) -> None, no blocker.
            with mock.patch("factonio.repos._run_gh", Gh({12: _pr_node()})):
                self.assertIsNone(inspect_pr_gate("acme/sample", 12)["trunk_drift"])

            with mock.patch("factonio.repos._run_gh", gh):
                same = inspect_pr_gate("acme/sample", 12, verified_head_sha=repo.head, check_trunk_drift=False)
                missing = inspect_pr_gate("acme/sample", 12, verified_head_sha="f" * 40, check_trunk_drift=False)
            self.assertEqual(
                {k: same["patch_continuity"][k] for k in ("ok", "continuous", "same_commit")},
                {"ok": True, "continuous": True, "same_commit": True},
            )
            self.assertFalse(missing["patch_continuity"]["ok"])
            self.assertFalse(missing["patch_continuity"]["continuous"])

    def test_merge_patch_continuity_is_opt_in(self):
        from factonio.repos import merge_pr

        with isolated_hermes_home() as home, _git_env():
            repo = Repo(home)
            repo.register()
            verified = repo.head
            repo.trunk({"c.txt": "unrelated\n"})
            _git(repo.seed, "checkout", "-q", "feat/x")
            _git(repo.seed, "rebase", "-q", "main")
            rebased = _git(repo.seed, "rev-parse", "HEAD")
            _git(repo.seed, "push", "-q", "--force", "origin", "feat/x")
            _git(repo.seed, "checkout", "-q", "main")
            _git(repo.local, "fetch", "-q", "origin")
            views = {12: {"state": "MERGED", "mergedAt": "2026-11-26T00:00:00Z", "baseRefName": "main"}}

            gh = Gh({12: _pr_node(head_sha=rebased)}, views=views)
            with mock.patch("factonio.repos._run_gh", gh):
                strict = merge_pr("acme/sample", 12, expected_head_sha=verified)
            self.assertFalse(strict["ok"])
            self.assertIn("HEAD mismatch", strict["error"])
            self.assertTrue(strict["patch_continuity"]["continuous"])
            self.assertFalse(any(c[:2] == ["pr", "merge"] for c in gh.calls))

            with mock.patch("factonio.repos._run_gh", gh):
                carried = merge_pr("acme/sample", 12, expected_head_sha=verified, allow_patch_continuity=True)
            self.assertTrue(carried["merged"], carried)
            self.assertEqual(carried["head_sha"], rebased)
            self.assertEqual(carried["expected_head_sha"], verified)
            self.assertTrue(carried["patch_continuity"]["continuous"])
            merge_call = next(c for c in gh.calls if c[:2] == ["pr", "merge"])
            self.assertEqual(merge_call[merge_call.index("--match-head-commit") + 1], rebased)

            # A content change after verification is refused even with the opt-in.
            _git(repo.seed, "checkout", "-q", "feat/x")
            changed = _commit(repo.seed, {"a.txt": "a\nfeature\nsneaky\n"}, "post-verify")
            _git(repo.seed, "push", "-q", "origin", "feat/x")
            _git(repo.seed, "checkout", "-q", "main")
            _git(repo.local, "fetch", "-q", "origin")
            gh = Gh({12: _pr_node(head_sha=changed)}, views=views)
            with mock.patch("factonio.repos._run_gh", gh):
                refused = merge_pr("acme/sample", 12, expected_head_sha=verified, allow_patch_continuity=True)
            self.assertFalse(refused["ok"])
            self.assertIn("patch continuity not proven", refused["error"])
            self.assertFalse(any(c[:2] == ["pr", "merge"] for c in gh.calls))


    def test_non_ascii_and_special_paths_are_not_quoted(self):
        from factonio.repos import assess_trunk_drift, inspect_pr_gate

        workflow = ".github/workflows/déploy \"x\".yml"
        cases = [
            ({workflow: "on: push\n"}, "workflow_rebase_required", True, [workflow]),
            ({"café/uv.lock": "x\n", "naïve\tdir/package-lock.json": "{}\n"}, "lockfile_only", False, []),
        ]
        for files, policy, rebase, wf in cases:
            with self.subTest(policy=policy), isolated_hermes_home() as home, _git_env():
                repo = Repo(home)
                repo.trunk(files)
                res = assess_trunk_drift(repo.local, "origin/feat/x", fetch=True)
                self.assertTrue(res["ok"], res)
                self.assertEqual(res["trunk_files"], sorted(files))
                self.assertEqual((res["policy"], res["rebase_required"], res["workflow_files"]), (policy, rebase, wf))

                repo.register()
                with mock.patch("factonio.repos._run_gh", Gh({12: _pr_node(head_branch="feat/x", head_sha=repo.head)})):
                    gate = inspect_pr_gate("acme/sample", 12)
                self.assertEqual(gate["trunk_drift"]["policy"], policy)
                self.assertEqual(gate["ready_to_merge"], not rebase)
                self.assertEqual(gate["blocker_tier"], 1 if rebase else None)
                if rebase:
                    self.assertEqual(gate["blockers"], [f"Trunk drift requires rebase (workflow_rebase_required): {workflow}"])

    def _byte_commit(self, repo: Path, branch: str, files: dict[bytes, bytes], msg: str) -> str:
        _git(repo, "checkout", "-q", branch)
        for name, content in files.items():
            path = os.path.join(os.fsencode(repo), name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            try:
                with open(path, "wb") as fh:
                    fh.write(content)
            except OSError as exc:  # e.g. filesystems that reject non-UTF-8 names
                self.skipTest(f"filesystem rejects non-UTF-8 filename {name!r}: {exc}")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", msg)
        _git(repo, "push", "-q", "origin", branch)
        sha = _git(repo, "rev-parse", "HEAD")
        _git(repo, "checkout", "-q", "main")
        return sha

    def test_non_utf8_trunk_filename_does_not_fail_open(self):
        from factonio.repos import assess_trunk_drift, inspect_pr_gate, merge_pr

        cases = [
            ({b"asset-\xff.txt": b"x\n", b".github/workflows/ci.yml": b"on: pull_request\n"},
             "workflow_rebase_required", ".github/workflows/ci.yml"),
            ({b"asset-\xfe.txt": b"x\n", b"a.txt": b"trunk\n"}, "overlap_rebase_required", "a.txt"),
        ]
        for files, policy, blocker_path in cases:
            with self.subTest(policy=policy), isolated_hermes_home() as home, _git_env():
                repo = Repo(home)
                self._byte_commit(repo.seed, "main", files, "trunk")
                res = assess_trunk_drift(repo.local, "origin/feat/x", fetch=True)
                self.assertTrue(res["ok"], res)
                self.assertEqual((res["policy"], res["rebase_required"]), (policy, True))
                asset = next(n for n in files if n.startswith(b"asset-"))
                self.assertIn(asset.decode("utf-8", "replace"), res["trunk_files"])
                json.dumps(res).encode("utf-8")  # display lists are serializable UTF-8 (no lone surrogates)

                repo.register()
                node = _pr_node(head_branch="feat/x", head_sha=repo.head)
                with mock.patch("factonio.repos._run_gh", Gh({12: node})):
                    gate = inspect_pr_gate("acme/sample", 12)
                self.assertEqual(gate["trunk_drift"]["policy"], policy)
                self.assertFalse(gate["ready_to_merge"])
                self.assertEqual(gate["blocker_tier"], 1)
                self.assertEqual(gate["blockers"], [f"Trunk drift requires rebase ({policy}): {blocker_path}"])

                views = {12: {"state": "MERGED", "mergedAt": "2026-11-26T00:00:00Z", "baseRefName": "main"}}
                gh = Gh({12: node}, views=views)
                with mock.patch("factonio.repos._run_gh", gh):
                    refused = merge_pr("acme/sample", 12, expected_head_sha=repo.head)
                self.assertFalse(refused["ok"], refused)
                self.assertFalse(any(c[:2] == ["pr", "merge"] for c in gh.calls))

                code, out = TestCliAndTools()._cli({12: node}, "--json", "pr-gate", "acme/sample", "12")
                self.assertEqual(code, 1, out)
                data = json.loads(out)
                self.assertFalse(data["ready_to_merge"])
                self.assertEqual(data["trunk_drift"]["policy"], policy)

    def test_non_utf8_paths_compare_as_raw_bytes(self):
        from factonio.repos import assess_trunk_drift

        cases = [
            ({b"bin-\xff.dat": b"t\n"}, {b"bin-\xfe.dat": b"b\n"}, "disjoint", False, []),
            ({b"shared-\xff.dat": b"t\n"}, {b"shared-\xff.dat": b"b\n"}, "overlap_rebase_required", True,
             [b"shared-\xff.dat".decode("utf-8", "replace")]),
        ]
        for trunk, branch, policy, overlap, overlapping in cases:
            with self.subTest(policy=policy), isolated_hermes_home() as home, _git_env():
                repo = Repo(home)
                self._byte_commit(repo.seed, "feat/x", branch, "branch")
                self._byte_commit(repo.seed, "main", trunk, "trunk")
                _git(repo.local, "fetch", "-q", "origin")
                res = assess_trunk_drift(repo.local, "origin/feat/x", fetch=True)
                self.assertTrue(res["ok"], res)
                self.assertEqual(res["pr_files"], sorted(["a.txt", *(n.decode("utf-8", "replace") for n in branch)]))
                self.assertEqual((res["policy"], res["has_overlap"], res["overlapping_files"]),
                                 (policy, overlap, overlapping))
                json.dumps(res).encode("utf-8")

    def test_fork_head_branch_colliding_with_origin_branch_fetches_pull_ref(self):
        from factonio.repos import inspect_pr_gate

        with isolated_hermes_home() as home, _git_env():
            repo = Repo(home)
            repo.register()
            # Fork PR: its head lives only at refs/pull/12/head; headRefName "main" collides with origin/main.
            _git(repo.seed, "checkout", "-q", "-b", "fork-main", "main")
            fork_head = _commit(repo.seed, {"b.txt": "b\nfork\n"}, "fork change")
            _git(repo.seed, "push", "-q", "origin", "fork-main:refs/pull/12/head")
            _git(repo.seed, "checkout", "-q", "main")
            repo.trunk({"b.txt": "b\ntrunk\n"})
            with self.assertRaises(AssertionError):
                _git(repo.local, "cat-file", "-e", f"{fork_head}^{{commit}}")

            node = _pr_node(head_branch="main", head_sha=fork_head)
            with mock.patch("factonio.repos._run_gh", Gh({12: node})):
                gate = inspect_pr_gate("acme/sample", 12, fetch=True)
            self.assertIsNotNone(gate["trunk_drift"], gate)
            self.assertEqual(gate["trunk_drift"]["policy"], "overlap_rebase_required")
            self.assertEqual(gate["trunk_drift"]["overlapping_files"], ["b.txt"])
            self.assertEqual(gate["blocker_tier"], 1)
            self.assertFalse(gate["ready_to_merge"])
            self.assertEqual(_git(repo.local, "rev-parse", "refs/factonio/pull/12/head"), fork_head)

    def test_fetch_applies_to_patch_continuity_without_trunk_drift(self):
        from factonio.repos import inspect_pr_gate, merge_pr

        with isolated_hermes_home() as home, _git_env():
            repo = Repo(home)
            repo.register()
            verified = repo.head
            _git(repo.local, "fetch", "-q", "origin")  # verified commit is local
            repo.trunk({"c.txt": "unrelated\n"})
            _git(repo.seed, "checkout", "-q", "feat/x")
            _git(repo.seed, "rebase", "-q", "main")
            rebased = _git(repo.seed, "rev-parse", "HEAD")
            _git(repo.seed, "push", "-q", "--force", "origin", "feat/x")
            _git(repo.seed, "checkout", "-q", "main")
            node = _pr_node(head_branch="feat/x", head_sha=rebased)
            views = {12: {"state": "MERGED", "mergedAt": "2026-11-26T00:00:00Z", "baseRefName": "main"}}

            with mock.patch("factonio.repos._run_gh", Gh({12: node})):
                unfetched = inspect_pr_gate("acme/sample", 12, verified_head_sha=verified, check_trunk_drift=False)
            self.assertFalse(unfetched["patch_continuity"]["ok"])
            self.assertIn("not available locally", unfetched["patch_continuity"]["error"])

            with mock.patch("factonio.repos._run_gh", Gh({12: node})):
                fetched = inspect_pr_gate(
                    "acme/sample", 12, verified_head_sha=verified, check_trunk_drift=False, fetch=True,
                )
            self.assertIsNone(fetched["trunk_drift"])
            self.assertTrue(fetched["patch_continuity"]["ok"], fetched["patch_continuity"])
            self.assertTrue(fetched["patch_continuity"]["continuous"])

        with isolated_hermes_home() as home, _git_env():
            repo = Repo(home)
            repo.register()
            verified = repo.head
            _git(repo.local, "fetch", "-q", "origin")
            repo.trunk({"c.txt": "unrelated\n"})
            _git(repo.seed, "checkout", "-q", "feat/x")
            _git(repo.seed, "rebase", "-q", "main")
            rebased = _git(repo.seed, "rev-parse", "HEAD")
            _git(repo.seed, "push", "-q", "--force", "origin", "feat/x")
            _git(repo.seed, "checkout", "-q", "main")
            gh = Gh({12: _pr_node(head_branch="feat/x", head_sha=rebased)}, views=views)
            with mock.patch("factonio.repos._run_gh", gh):
                merged = merge_pr(
                    "acme/sample", 12, expected_head_sha=verified, allow_patch_continuity=True,
                    check_trunk_drift=False, fetch=True,
                )
            self.assertTrue(merged["merged"], merged)
            self.assertEqual(merged["head_sha"], rebased)
            self.assertTrue(merged["patch_continuity"]["continuous"])


def _stack_prs(**overrides: dict) -> dict[int, dict]:
    prs = {
        10: _pr_node(10, head_branch="s/1", head_sha="1" * 40, base_branch="main"),
        11: _pr_node(11, head_branch="s/2", head_sha="2" * 40, base_branch="s/1"),
        12: _pr_node(12, head_branch="s/3", head_sha="3" * 40, base_branch="s/2"),
    }
    for key, fields in overrides.items():
        n = int(key.lstrip("p"))
        prs[n] = _pr_node(n, head_branch=prs[n]["headRefName"], head_sha=prs[n]["headRefOid"],
                          base_branch=prs[n]["baseRefName"], **fields)
    return prs


class TestStackGate(unittest.TestCase):
    def _stack(self, prs: dict[int, dict], numbers: Any) -> dict:
        from factonio.repos import inspect_stack_gate

        with mock.patch("factonio.repos._run_gh", Gh(prs)):
            return inspect_stack_gate("acme/sample", numbers)

    def test_all_ready_orders_bottom_to_top(self):
        with isolated_hermes_home():
            res = self._stack(_stack_prs(), [12, 10, 11])
        self.assertTrue(res["ok"])
        self.assertEqual(res["order"], [10, 11, 12])
        self.assertTrue(res["ready_to_merge"])
        self.assertEqual(res["contiguous_ready_run"], [10, 11, 12])
        self.assertIsNone(res["ceiling"])
        self.assertIsNone(res["highest_blocker_tier"])
        self.assertIsNone(res["lowest_pending_frontier"])
        self.assertEqual(res["next_action"]["action"], "merge_bottom")
        self.assertEqual(res["next_action"]["pr_number"], 10)
        self.assertEqual(set(res["tiers_summary"]), {"tier_1", "tier_2", "tier_3_failed", "tier_4", "tier_3_pending"})

    def test_tier_major_order_across_stack(self):
        prs = _stack_prs(
            p10={"isDraft": True},
            p11={"contexts": [_check("ci", None, "QUEUED")], "rollup_state": "PENDING"},
            p12={"mergeable": "CONFLICTING"},
        )
        with isolated_hermes_home():
            res = self._stack(prs, [10, 11, 12])
        # Tier 1 on the tip outranks the bottom's Tier 4 and the middle's pending checks.
        self.assertEqual(res["next_action"]["tier"], "tier_1")
        self.assertEqual(res["next_action"]["pr_number"], 12)
        self.assertEqual(res["highest_blocker_tier"], 1)
        self.assertEqual([e["pr_number"] for e in res["tiers_summary"]["tier_4"]], [10])
        self.assertEqual([e["pr_number"] for e in res["tiers_summary"]["tier_3_pending"]], [11])
        frontier = res["lowest_pending_frontier"]
        self.assertEqual(frontier["pr_number"], 11)
        self.assertEqual(frontier["pending_checks"], ["ci"])
        self.assertEqual(frontier["pending_review_automations"], [])
        self.assertFalse(frontier["review_automation_running"])
        self.assertEqual(
            (frontier["head_branch"], frontier["base_branch"], frontier["head_sha"]), ("s/2", "s/1", "2" * 40),
        )
        self.assertEqual(res["contiguous_ready_run"], [])
        ceiling = res["ceiling"]
        self.assertEqual(ceiling["pr_number"], 10)
        self.assertEqual(ceiling["blocker_tier"], 4)
        self.assertEqual(ceiling["blockers"], ["PR is still a draft"])
        self.assertEqual((ceiling["head_branch"], ceiling["base_branch"], ceiling["head_sha"]), ("s/1", "main", "1" * 40))
        self.assertFalse(res["ready_to_merge"])

        # Pending is the lowest priority: a failed CI check above it wins next_action.
        prs = _stack_prs(
            p10={"contexts": [_check("ci", None, "QUEUED")], "rollup_state": "PENDING"},
            p12={"contexts": [_check("ci", "FAILURE")], "rollup_state": "FAILURE"},
        )
        with isolated_hermes_home():
            res = self._stack(prs, [10, 11, 12])
        self.assertEqual((res["next_action"]["tier"], res["next_action"]["pr_number"]), ("tier_3_failed", 12))
        self.assertEqual(res["lowest_pending_frontier"]["pr_number"], 10)

        # Ready prefix then a ceiling.
        with isolated_hermes_home():
            res = self._stack(_stack_prs(p12={"reviewDecision": "CHANGES_REQUESTED"}), [10, 11, 12])
        self.assertEqual(res["contiguous_ready_run"], [10, 11])
        self.assertEqual(res["ceiling"]["pr_number"], 12)
        self.assertEqual(res["ceiling"]["blocker_tier"], 4)
        self.assertIsNone(res["lowest_pending_frontier"])
        self.assertEqual(res["next_action"]["action"], "human_gate")

        # Review automation alone makes a PR the pending frontier.
        bot = _check("Cursor Bugbot", None, "IN_PROGRESS")
        with isolated_hermes_home():
            res = self._stack(_stack_prs(p11={"contexts": [_check("ci"), bot], "rollup_state": "PENDING"}), [10, 11, 12])
        frontier = res["lowest_pending_frontier"]
        self.assertEqual(frontier["pr_number"], 11)
        self.assertTrue(frontier["review_automation_running"])
        self.assertEqual(frontier["pending_review_automations"], ["Cursor Bugbot"])

    def test_cycle_is_detected_without_looping(self):
        prs = {
            1: _pr_node(1, head_branch="x", head_sha="1" * 40, base_branch="y"),
            2: _pr_node(2, head_branch="y", head_sha="2" * 40, base_branch="x"),
        }
        with isolated_hermes_home():
            res = self._stack(prs, [1, 2])
        self.assertTrue(res["ok"])
        self.assertTrue(res["cycle_detected"])
        self.assertEqual(res["order"], [1, 2])
        self.assertFalse(res["ready_to_merge"])
        self.assertEqual(res["next_action"]["action"], "fix_stack_cycle")

    def test_validation_and_errors(self):
        from factonio.repos import inspect_stack_gate

        with isolated_hermes_home():
            for bad in ([], [0], ["x"], [True], "12,13", None):
                with self.assertRaises(ValueError, msg=repr(bad)):
                    inspect_stack_gate("acme/sample", bad)
            res = self._stack(_stack_prs(), [10, 99])
        self.assertFalse(res["ok"])
        self.assertIn("PR #99", res["error"])
        self.assertFalse(res["ready_to_merge"])


class TestCliAndTools(unittest.TestCase):
    def _cli(self, prs: dict[int, dict], *argv: str) -> tuple[int, str]:
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
        with mock.patch("factonio.repos._run_gh", Gh(prs)), contextlib.redirect_stdout(out):
            try:
                handler_fn(parser.parse_args(list(argv)))
            except SystemExit as exc:
                code = int(exc.code or 0)
        return code, out.getvalue()

    def _tools(self) -> dict:
        from factonio.agent_tools import register_tools

        class _Ctx:
            tools: dict = {}

            def register_tool(self, *, name, toolset, schema, handler, **kw):
                self.tools[name] = {"schema": schema, "handler": handler}

        ctx = _Ctx()
        register_tools(ctx)
        return ctx.tools

    def test_cli_stack_and_single_gate(self):
        with isolated_hermes_home():
            code, out = self._cli(_stack_prs(), "pr-gate", "acme/sample", "--stack", "12,11,10")
            self.assertEqual(code, 0, out)
            self.assertIn("READY: acme/sample stack #10 -> #11 -> #12", out)
            self.assertIn("Next action: merge_bottom on #10", out)

            code, out = self._cli(_stack_prs(p11={"isDraft": True}), "--json", "pr-gate", "acme/sample", "--stack", "10,11,12")
            self.assertEqual(code, 1)
            data = json.loads(out)
            self.assertEqual(data["ceiling"]["pr_number"], 11)
            self.assertEqual(data["ceiling"]["blocker_tier"], 4)
            self.assertIsNone(data["lowest_pending_frontier"])

            pending = _stack_prs(p11={"contexts": [_check("ci", None, "QUEUED")], "rollup_state": "PENDING"})
            code, out = self._cli(pending, "pr-gate", "acme/sample", "--stack", "10,11,12")
            self.assertEqual(code, 1)
            self.assertIn("Ready run: #10; ceiling: #11; pending frontier: #11", out)
            self.assertEqual(data["contiguous_ready_run"], [10])

            code, out = self._cli(_stack_prs(), "pr-gate", "acme/sample", "--stack", "10,abc")
            self.assertEqual(code, 1)
            self.assertIn("Error:", out)

            code, out = self._cli(_stack_prs(), "pr-gate", "acme/sample")
            self.assertEqual(code, 1)
            self.assertIn("pass a pr_number or --stack", out)

            node = _pr_node(10, contexts=[_check("ci"), _check("Code Review Gate", "ACTION_REQUIRED")])
            code, out = self._cli({10: node}, "pr-gate", "acme/sample", "10")
            self.assertEqual(code, 1)
            self.assertIn("BLOCKED (tier 4)", out)
            self.assertIn("Approval gates: Code Review Gate", out)

    def test_tool_schemas_and_stack_handler(self):
        with isolated_hermes_home():
            tools = self._tools()
            gate_schema = tools["factonio_pr_gate"]["schema"]["parameters"]
            self.assertEqual(gate_schema["required"], ["repo"])
            for key in ("pr_number", "stack_pr_numbers", "verified_head_sha", "fetch"):
                self.assertIn(key, gate_schema["properties"])
            merge_props = tools["factonio_merge_pr"]["schema"]["parameters"]["properties"]
            for key in ("allow_stacked_merge", "allow_patch_continuity", "fetch"):
                self.assertIn(key, merge_props)
                self.assertFalse(merge_props[key]["default"])

            handler = tools["factonio_pr_gate"]["handler"]
            self.assertFalse(json.loads(handler({"repo": "acme/sample"}))["ok"])
            with mock.patch("factonio.repos._run_gh", Gh(_stack_prs())):
                stack = json.loads(handler({"repo": "acme/sample", "stack_pr_numbers": [11, 10, 12]}))
                single = json.loads(handler({"repo": "acme/sample", "pr_number": 10}))
            self.assertEqual(stack["order"], [10, 11, 12])
            self.assertTrue(stack["ready_to_merge"])
            self.assertTrue(single["ready_to_merge"])
            self.assertIn("approval_gates", single)

            from factonio import trail

            rows = [r for r in trail.query_decisions(limit=50) if r["decision"] == "pr_gate"]
            self.assertTrue(any((r.get("evidence") or {}).get("stack_pr_numbers") == [11, 10, 12] for r in rows), rows)

    def test_merge_tool_refuses_stacked_by_default(self):
        with isolated_hermes_home():
            handler = self._tools()["factonio_merge_pr"]["handler"]
            views = {11: {"state": "MERGED", "mergedAt": "2026-11-26T00:00:00Z", "baseRefName": "s/1"}}
            gh = Gh(_stack_prs(), views=views)
            with mock.patch("factonio.repos._run_gh", gh):
                refused = json.loads(handler({"repo": "acme/sample", "pr_number": 11}))
                forced = json.loads(handler({"repo": "acme/sample", "pr_number": 11, "allow_stacked_merge": True}))
            self.assertFalse(refused["ok"])
            self.assertIn("stacked", refused["error"])
            self.assertTrue(forced["merged"], forced)

            from factonio import trail

            rows = [r for r in trail.query_decisions(limit=50) if r["decision"] == "merge_pr"]
            flags = sorted(bool((r.get("evidence") or {}).get("allow_stacked_merge")) for r in rows)
            self.assertEqual(flags, [False, True])


class TestMergeTrailAudit(unittest.TestCase):
    """Cross-unit: refused merges must not read as gate loosening in `trail --audit`."""

    CTX = {"repo": "acme/sample", "tenant": "demo-stack", "card_id": "t_demo", "actor": "orchestrator"}

    def _handler(self):
        from factonio.agent_tools import register_tools

        tools: dict[str, Any] = {}

        class _Ctx:
            def register_tool(self, **kw: Any) -> None:
                tools[kw["name"]] = kw

            def __getattr__(self, name: str) -> Any:
                return lambda *a, **k: None

        register_tools(_Ctx())
        return tools["factonio_merge_pr"]["handler"]

    def _audit(self) -> dict:
        from factonio import trail

        return trail.audit_trail_evidence(tenant=self.CTX["tenant"])

    def test_refused_stacked_merge_then_bottom_up_is_clean(self):
        with isolated_hermes_home():
            handler = self._handler()
            prs = _stack_prs()
            views = {10: {"state": "MERGED", "mergedAt": "2026-11-26T00:00:00Z", "baseRefName": "main"},
                     11: {"state": "MERGED", "mergedAt": "2026-11-26T00:01:00Z", "baseRefName": "main"}}
            with mock.patch("factonio.repos._run_gh", Gh(prs, views=views)):
                refused = json.loads(handler({**self.CTX, "pr_number": 11}))
            self.assertFalse(refused["ok"])
            self.assertIn("allow_stacked_merge=True", refused["error"])

            restacked = dict(prs)
            restacked[11] = _pr_node(11, head_branch="s/2", head_sha="2" * 40, base_branch="main")
            with mock.patch("factonio.repos._run_gh", Gh(restacked, views=views)):
                first = json.loads(handler({**self.CTX, "pr_number": 10}))
                second = json.loads(handler({**self.CTX, "pr_number": 11}))
            self.assertTrue(first["merged"], first)
            self.assertTrue(second["merged"], second)

            audit = self._audit()
            self.assertEqual(audit["counts"]["gate_loosening"], 0, audit)
            self.assertTrue(audit["clean"], audit)

    def test_refused_head_mismatch_without_continuity_is_not_loosening(self):
        with isolated_hermes_home():
            handler = self._handler()
            with mock.patch("factonio.repos._run_gh", Gh({12: _pr_node(12)})):
                refused = json.loads(handler({**self.CTX, "pr_number": 12, "expected_head_sha": "b" * 40,
                                              "method": "force_merge"}))
            self.assertFalse(refused["ok"])
            self.assertIn("HEAD mismatch", refused["error"])
            self.assertEqual(self._audit()["counts"]["gate_loosening"], 0)

    @unittest.skipUnless(shutil.which("git"), "git is required")
    def test_patch_continuity_override_is_flagged_but_refused_mismatch_is_not(self):
        with isolated_hermes_home() as home, _git_env():
            handler = self._handler()
            repo = Repo(home)
            repo.register()
            verified = repo.head
            repo.trunk({"c.txt": "unrelated\n"})
            _git(repo.seed, "checkout", "-q", "feat/x")
            _git(repo.seed, "rebase", "-q", "main")
            rebased = _git(repo.seed, "rev-parse", "HEAD")
            _git(repo.seed, "push", "-q", "--force", "origin", "feat/x")
            _git(repo.seed, "checkout", "-q", "main")
            _git(repo.local, "fetch", "-q", "origin")
            views = {12: {"state": "MERGED", "mergedAt": "2026-11-26T00:00:00Z", "baseRefName": "main"}}
            args = {**self.CTX, "pr_number": 12, "expected_head_sha": verified}

            with mock.patch("factonio.repos._run_gh", Gh({12: _pr_node(head_sha=rebased)}, views=views)):
                refused = json.loads(handler(args))
            self.assertFalse(refused["ok"])
            self.assertIn("HEAD mismatch", refused["error"])
            self.assertEqual(self._audit()["counts"]["gate_loosening"], 0)

            with mock.patch("factonio.repos._run_gh", Gh({12: _pr_node(head_sha=rebased)}, views=views)):
                carried = json.loads(handler({**args, "allow_patch_continuity": True}))
            self.assertTrue(carried["merged"], carried)
            audit = self._audit()
            self.assertEqual(audit["counts"]["gate_loosening"], 1, audit)
            finding = next(f for f in audit["findings"] if f["kind"] == "gate_loosening")
            self.assertEqual(finding["tokens"], ["allow_patch_continuity"])
            self.assertEqual(finding["result"], "merged")

    def test_explicit_stacked_override_is_flagged(self):
        with isolated_hermes_home():
            handler = self._handler()
            views = {11: {"state": "MERGED", "mergedAt": "2026-11-26T00:00:00Z", "baseRefName": "s/1"}}
            with mock.patch("factonio.repos._run_gh", Gh(_stack_prs(), views=views)):
                forced = json.loads(handler({**self.CTX, "pr_number": 11, "allow_stacked_merge": True}))
            self.assertTrue(forced["merged"], forced)
            audit = self._audit()
            self.assertFalse(audit["clean"])
            self.assertEqual(audit["counts"]["gate_loosening"], 1, audit)


if __name__ == "__main__":
    unittest.main()
