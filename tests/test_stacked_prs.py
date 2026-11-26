"""Clean-room real-git tests for stacked PRs: base_branch landing, true-base gate/merge, restack continuity."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest import mock

from tests._env import PLUGIN_ROOT, isolated_hermes_home

if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))


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


def _proc(stdout: str = "", rc: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["gh"], returncode=rc, stdout=stdout, stderr=stderr)


_LINES = "".join(f"line {i}\n" for i in range(1, 11))


def _edit_line(text: str, n: int, new: str) -> str:
    lines = text.splitlines(keepends=True)
    lines[n - 1] = new + "\n"
    return "".join(lines)


class StackFixture:
    """Bare origin + upstream `seed` clone (simulates GitHub) + registered operator clone `local`.

    origin/main: init; feat/parent: 2 commits on main; feat/child: 1 commit on feat/parent.
    """

    def __init__(self, home: Path) -> None:
        self.home = home
        self.origin = home / "origin.git"
        _git(home, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.seed = home / "seed"
        _git(home, "clone", "-q", str(self.origin), str(self.seed))
        _git(self.seed, "symbolic-ref", "HEAD", "refs/heads/main")
        self.init_sha = _commit(self.seed, {"a.txt": _LINES, "README": "hello\n"}, "init")
        _git(self.seed, "push", "-q", "origin", "main")
        _git(self.seed, "checkout", "-q", "-b", "feat/parent")
        _commit(self.seed, {"p.txt": "p1\n"}, "parent 1")
        self.parent_tip = _commit(self.seed, {"p.txt": "p1\np2\n"}, "parent 2")
        _git(self.seed, "push", "-q", "origin", "feat/parent")
        _git(self.seed, "checkout", "-q", "-b", "feat/child")
        self.child_sha = _commit(self.seed, {"a.txt": _edit_line(_LINES, 5, "child line 5")}, "child")
        _git(self.seed, "push", "-q", "origin", "feat/child")
        _git(self.seed, "checkout", "-q", "main")
        self.local = home / "dev" / "sample"
        self.local.parent.mkdir(parents=True, exist_ok=True)
        _git(home, "clone", "-q", str(self.origin), str(self.local))

    def add_child(self, branch: str, files: dict[str, str]) -> str:
        _git(self.seed, "checkout", "-q", "-B", branch, self.parent_tip)
        sha = _commit(self.seed, files, branch)
        _git(self.seed, "push", "-q", "origin", branch)
        _git(self.seed, "checkout", "-q", "main")
        return sha

    def squash_merge_parent(self, *, main_edit: Optional[tuple[int, str]] = None) -> str:
        """Squash-merge feat/parent into main, add a later main commit, and delete feat/parent."""
        _git(self.seed, "checkout", "-q", "main")
        _git(self.seed, "merge", "-q", "--squash", "feat/parent")
        _git(self.seed, "commit", "-q", "-m", "Parent (#5)")
        if main_edit is not None:
            text = (self.seed / "a.txt").read_text(encoding="utf-8")
            _commit(self.seed, {"a.txt": _edit_line(text, *main_edit)}, "main edit")
        else:
            _commit(self.seed, {"u.txt": "unrelated\n"}, "unrelated")
        _git(self.seed, "push", "-q", "origin", "main")
        _git(self.seed, "push", "-q", "origin", "--delete", "feat/parent")
        _git(self.seed, "branch", "-q", "-D", "feat/parent")
        return _git(self.seed, "rev-parse", "main")

    def origin_ref(self, branch: str) -> str:
        return _git(self.origin, "rev-parse", f"refs/heads/{branch}")


def _pr_node(number: int, head_branch: str, head_sha: str, base_branch: str, state: str = "OPEN") -> dict:
    return {
        "number": number,
        "url": f"https://github.com/acme/sample/pull/{number}",
        "title": f"PR {number}",
        "state": state,
        "isDraft": False,
        "headRefName": head_branch,
        "headRefOid": head_sha,
        "baseRefName": base_branch,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "reviewDecision": "APPROVED",
        "reviewThreads": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []},
        "commits": {"nodes": [{"commit": {"oid": head_sha, "statusCheckRollup": {
            "state": "SUCCESS",
            "contexts": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [
                {"__typename": "CheckRun", "name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"},
            ]},
        }}}]},
    }


class StackGh:
    """Fake `gh` serving GraphQL gates per PR number, parent `pr view`, `pr edit`, `pr list`, and merge."""

    def __init__(self, prs: dict[int, dict], *, default_branch: Optional[str] = "main",
                 views: Optional[dict[int, dict]] = None, children: Optional[list[dict]] = None,
                 edit_rc: int = 0) -> None:
        self.prs = prs
        self.default_branch = default_branch
        self.views = views or {}
        self.children = children or []
        self.edit_rc = edit_rc
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        if args[:2] == ["api", "graphql"]:
            number = next(int(a.split("=", 1)[1]) for a in args if a.startswith("number="))
            repo: dict[str, Any] = {"pullRequest": self.prs[number]}
            if self.default_branch:
                repo["defaultBranchRef"] = {"name": self.default_branch}
            return _proc(json.dumps({"data": {"repository": repo}}))
        if args[:2] == ["pr", "view"]:
            return _proc(json.dumps(self.views[int(args[2])]))
        if args[:2] == ["pr", "edit"]:
            return _proc("", rc=self.edit_rc, stderr="edit failed" if self.edit_rc else "")
        if args[:2] == ["pr", "list"]:
            return _proc(json.dumps(self.children))
        if args[:2] == ["pr", "merge"]:
            return _proc("")
        raise AssertionError(f"unexpected gh call {args}")

    def edits(self) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["pr", "edit"]]


def _register(local: Path) -> None:
    from factonio.repos import add_repo

    add_repo("acme/sample", local_path=str(local), clone=False, inspect_remote=False)


def _worktree_count(repo: Path) -> int:
    return sum(1 for line in _git(repo, "worktree", "list", "--porcelain").splitlines() if line.startswith("worktree "))


def _checkout_state(repo: Path) -> tuple[str, str, str]:
    return (
        _git(repo, "rev-parse", "--abbrev-ref", "HEAD"),
        _git(repo, "rev-parse", "HEAD"),
        _git(repo, "status", "--porcelain"),
    )


def _restack_dirs() -> set[str]:
    return {p.name for p in Path(tempfile.gettempdir()).glob("factonio-restack-*")}


def _trail_rows(decision: str) -> list[dict]:
    from factonio import trail

    return [r for r in trail.query_decisions(limit=500) if r["decision"] == decision]


@unittest.skipUnless(shutil.which("git"), "git is required")
class TestSeedAndStackedSpawn(unittest.TestCase):
    def test_seed_stacked_branch_resolution_and_failures(self):
        from factonio.repos import seed_stacked_branch

        with isolated_hermes_home() as home, _git_env():
            fx = StackFixture(home)
            before = _checkout_state(fx.local)

            remote_only = seed_stacked_branch(fx.local, "feat/parent", "sample/stack-a")
            self.assertEqual(
                (remote_only["ok"], remote_only["seeded"], remote_only["base_ref"], remote_only["base_commit"]),
                (True, True, "refs/remotes/origin/feat/parent", fx.parent_tip),
            )
            self.assertEqual(_git(fx.local, "rev-parse", "refs/heads/sample/stack-a"), fx.parent_tip)

            _git(fx.local, "branch", "-q", "feat/parent", fx.init_sha)
            local_first = seed_stacked_branch(fx.local, "feat/parent", "sample/stack-b")
            self.assertEqual((local_first["base_ref"], local_first["base_commit"]), ("refs/heads/feat/parent", fx.init_sha))

            again = seed_stacked_branch(fx.local, "feat/parent", "sample/stack-a")
            self.assertEqual((again["ok"], again["seeded"], again.get("already_exists")), (True, False, True))

            missing = seed_stacked_branch(fx.local, "feat/nope", "sample/stack-c")
            self.assertEqual((missing["ok"], missing["seeded"]), (False, False))
            self.assertIn("not found", missing["reason"])
            self.assertFalse(_git(fx.local, "branch", "--list", "sample/stack-c"))

            not_repo = home / "plain"
            not_repo.mkdir()
            self.assertEqual(seed_stacked_branch(not_repo, "main", "x/y")["ok"], False)
            fake_git = home / "fake"
            (fake_git / ".git").mkdir(parents=True)
            self.assertEqual(seed_stacked_branch(fake_git, "main", "x/y")["seeded"], False)
            with self.assertRaises(ValueError):
                seed_stacked_branch(fx.local, "-evil", "x/y")

            self.assertEqual(_checkout_state(fx.local), before)

    def test_stacked_spawn_seeds_branch_and_unstacked_is_unchanged(self):
        from hermes_cli import kanban_db as kb
        from hermes_cli.kanban_db_connect import connect_closing
        from factonio.orchestrator import spawn_implementation_card

        with isolated_hermes_home() as home, _git_env():
            fx = StackFixture(home)
            _register(fx.local)

            plain = spawn_implementation_card("acme/sample", "acme-sample-t1", "Unit A", "Do A")
            self.assertEqual(
                (plain["base_branch"], plain["is_stacked"], plain["stack_parent_pr"], plain["branch_seeded"]),
                ("main", False, None, False),
            )

            stacked = spawn_implementation_card(
                "acme/sample", "acme-sample-t1", "Unit B", "Do B", base_branch="feat/parent", stack_parent_pr=5,
            )
            self.assertEqual(
                (stacked["base_branch"], stacked["is_stacked"], stacked["stack_parent_pr"], stacked["branch_seeded"]),
                ("feat/parent", True, 5, True),
            )
            branch = stacked["branch_name"]
            self.assertRegex(branch, r"^[a-z0-9-]+/stack-[0-9a-f]{8}$")
            self.assertEqual(_git(fx.local, "rev-parse", f"refs/heads/{branch}"), fx.parent_tip)
            self.assertEqual(_git(fx.local, "rev-parse", "--abbrev-ref", "HEAD"), "main")

            with connect_closing() as conn:
                plain_task = kb.get_task(conn, plain["task_id"])
                stacked_task = kb.get_task(conn, stacked["task_id"])
            assert plain_task and stacked_task and plain_task.body and stacked_task.body
            self.assertNotIn("Target base branch", plain_task.body)
            self.assertNotIn("Stacked PR", plain_task.body)
            self.assertIn("## Work Unit\nDo A\n\n## Information Barrier", plain_task.body)
            self.assertEqual(stacked_task.branch_name, branch)
            self.assertIn("- Target base branch: `feat/parent`", stacked_task.body)
            self.assertIn("- Stack parent PR: `acme/sample#5`", stacked_task.body)
            self.assertIn("gh pr create --base feat/parent", stacked_task.body)
            self.assertIn("origin/feat/parent...HEAD", stacked_task.body)

            rows = {r["card_id"]: r for r in _trail_rows("spawn_implement")}
            ev = rows[stacked["task_id"]]["evidence"]
            self.assertEqual(
                (ev["base_branch"], ev["is_stacked"], ev["stack_parent_pr"], ev["branch_seeded"]),
                ("feat/parent", True, 5, True),
            )
            self.assertEqual(rows[plain["task_id"]]["evidence"]["is_stacked"], False)

    def test_stacked_spawn_without_resolvable_base_does_not_pin_branch(self):
        from factonio.orchestrator import spawn_implementation_card
        from factonio.repos import add_repo

        with isolated_hermes_home() as home:
            local = home / "dev" / "fake"
            (local / ".git").mkdir(parents=True)
            add_repo("acme/fake", local_path=str(local), clone=False, inspect_remote=False)
            res = spawn_implementation_card("acme/fake", "acme-fake-t", "Unit", "Body", base_branch="feat/x")
            self.assertEqual((res["is_stacked"], res["branch_seeded"], res["branch_name"]), (True, False, None))
            for bad in ({"stack_parent_pr": 0}, {"stack_parent_pr": True}, {"base_branch": "a..b"}):
                with self.assertRaises(ValueError):
                    spawn_implementation_card("acme/fake", "acme-fake-t", "Unit", "Body", **bad)


@unittest.skipUnless(shutil.which("git"), "git is required")
class TestTrueBaseGateMergeAndPatchId(unittest.TestCase):
    def test_gate_reports_true_base_default_branch_and_is_stacked(self):
        from factonio.repos import inspect_pr_gate

        with isolated_hermes_home():
            gh = StackGh({7: _pr_node(7, "feat/child", "c" * 40, "feat/parent")})
            with mock.patch("factonio.repos._run_gh", gh):
                gate = inspect_pr_gate("acme/sample", 7)
            self.assertEqual((gate["base_branch"], gate["default_branch"], gate["is_stacked"]), ("feat/parent", "main", True))
            self.assertIn("defaultBranchRef", next(a for a in gh.calls[0] if a.startswith("query=")))
            self.assertTrue(gate["ready_to_merge"])

            legacy = StackGh({7: _pr_node(7, "feat/x", "c" * 40, "main")}, default_branch=None)
            with mock.patch("factonio.repos._run_gh", legacy):
                gate = inspect_pr_gate("acme/sample", 7)
            self.assertEqual((gate["default_branch"], gate["is_stacked"]), ("main", False))

    def test_merge_stacked_pr_syncs_true_base_and_honors_delete_branch(self):
        from factonio.repos import merge_pr

        with isolated_hermes_home() as home, _git_env():
            fx = StackFixture(home)
            _register(fx.local)
            # Upstream merges the child into feat/parent.
            _git(fx.seed, "checkout", "-q", "-B", "feat/parent", "origin/feat/parent")
            _git(fx.seed, "merge", "-q", "--ff-only", "origin/feat/child")
            _git(fx.seed, "push", "-q", "origin", "feat/parent")
            _git(fx.seed, "checkout", "-q", "main")
            gh = StackGh(
                {7: _pr_node(7, "feat/child", fx.child_sha, "feat/parent")},
                views={7: {"state": "MERGED", "mergedAt": "2026-11-25T00:00:00Z", "baseRefName": "feat/parent"}},
            )
            with mock.patch("factonio.repos._run_gh", gh):
                res = merge_pr(
                    "acme/sample", 7, expected_head_sha=fx.child_sha, delete_branch=False, allow_stacked_merge=True,
                )
            self.assertTrue(res["merged"], res)
            self.assertEqual((res["base_branch"], res["is_stacked"], res["delete_branch"]), ("feat/parent", True, False))
            merge_call = next(c for c in gh.calls if c[:2] == ["pr", "merge"])
            self.assertNotIn("--delete-branch", merge_call)
            self.assertTrue(res["local_synced"], res["local_sync_note"])
            self.assertIn("feat/parent", res["local_sync_note"])
            self.assertEqual(_git(fx.local, "rev-parse", "refs/heads/feat/parent"), fx.child_sha)
            self.assertEqual(_git(fx.local, "rev-parse", "--abbrev-ref", "HEAD"), "main")

            with mock.patch("factonio.repos._run_gh", gh):
                default = merge_pr("acme/sample", 7, allow_stacked_merge=True)
            self.assertIn("--delete-branch", [c for c in gh.calls if c[:2] == ["pr", "merge"]][-1])
            self.assertTrue(default["delete_branch"])

    def test_merge_refuses_stacked_pr_by_default(self):
        from factonio.repos import merge_pr

        with isolated_hermes_home():
            gh = StackGh(
                {7: _pr_node(7, "feat/child", "c" * 40, "feat/parent")},
                views={7: {"state": "MERGED", "mergedAt": "2026-11-25T00:00:00Z", "baseRefName": "feat/parent"}},
            )
            with mock.patch("factonio.repos._run_gh", gh):
                res = merge_pr("acme/sample", 7, expected_head_sha="c" * 40)
            self.assertFalse(res["ok"])
            self.assertFalse(res["merged"])
            self.assertFalse(res["queued"])
            self.assertIn("stacked", res["error"])
            self.assertIn("allow_stacked_merge", res["error"])
            self.assertTrue(res["gate"]["is_stacked"])
            self.assertFalse(any(c[:2] == ["pr", "merge"] for c in gh.calls))

            # Explicit override merges into the parent branch; unstacked PRs never need it.
            with mock.patch("factonio.repos._run_gh", gh):
                forced = merge_pr("acme/sample", 7, expected_head_sha="c" * 40, allow_stacked_merge=True)
            self.assertTrue(forced["merged"], forced)
            plain = StackGh(
                {8: _pr_node(8, "feat/x", "d" * 40, "main")},
                views={8: {"state": "MERGED", "mergedAt": "2026-11-25T00:00:00Z", "baseRefName": "main"}},
            )
            with mock.patch("factonio.repos._run_gh", plain):
                self.assertTrue(merge_pr("acme/sample", 8, expected_head_sha="d" * 40)["merged"])

    def test_inspect_pr_patch_id_and_drift_use_true_base(self):
        from factonio.repos import assess_trunk_drift, compute_patch_id, inspect_pr_patch_id

        with isolated_hermes_home() as home, _git_env():
            fx = StackFixture(home)
            _register(fx.local)
            gh = StackGh({7: _pr_node(7, "feat/child", fx.child_sha, "feat/parent")})
            with mock.patch("factonio.repos._run_gh", gh):
                res = inspect_pr_patch_id("acme/sample", 7, fetch=True)
            self.assertTrue(res["ok"], res)
            self.assertEqual((res["base_branch"], res["base_ref"], res["merge_base"]), ("feat/parent", "origin/feat/parent", fx.parent_tip))
            expected = compute_patch_id(fx.seed, fx.child_sha, base_ref=fx.parent_tip)
            self.assertEqual(res["patch_id"], expected)
            self.assertNotEqual(res["patch_id"], compute_patch_id(fx.local, fx.child_sha, base_ref="origin/main"))

            drift = assess_trunk_drift(fx.local, fx.child_sha, base_branch="feat/parent", default_branch="main")
            self.assertEqual((drift["trunk_ref"], drift["merge_base"], drift["pr_files"]), ("origin/feat/parent", fx.parent_tip, ["a.txt"]))
            fallback = assess_trunk_drift(fx.local, fx.child_sha, default_branch="main")
            self.assertEqual(fallback["trunk_ref"], "origin/main")


def _child_gh(fx: StackFixture) -> StackGh:
    """PR #7 feat/child -> feat/parent, with parent PR #5 (feat/parent -> main) already merged."""
    return StackGh(
        {7: _pr_node(7, "feat/child", fx.child_sha, "feat/parent")},
        views={5: {"headRefOid": fx.parent_tip, "headRefName": "feat/parent", "baseRefName": "main", "state": "MERGED"}},
    )


@unittest.skipUnless(shutil.which("git"), "git is required")
class TestRestack(unittest.TestCase):
    def _assert_clean(self, fx: StackFixture, before: tuple, tmp_before: set[str]) -> None:
        self.assertEqual(_checkout_state(fx.local), before)
        self.assertEqual(_worktree_count(fx.local), 1)
        self.assertEqual(_restack_dirs() - tmp_before, set())

    def test_squash_merged_deleted_parent_restacks_with_identical_patch_id(self):
        from factonio.repos import restack_pr

        with isolated_hermes_home() as home, _git_env():
            fx = StackFixture(home)
            _register(fx.local)
            new_main = fx.squash_merge_parent()
            before, tmp_before = _checkout_state(fx.local), _restack_dirs()
            gh = _child_gh(fx)
            with mock.patch("factonio.repos._run_gh", gh):
                res = restack_pr("acme/sample", 7, parent_pr=5, tenant="acme-sample-t1", card_id="t_orch")

            self.assertTrue(res["ok"], res)
            self.assertEqual((res["rebase_mode"], res["old_base_commit"], res["old_base_source"]), ("onto", fx.parent_tip, "parent_pr #5"))
            self.assertEqual((res["target_base"], res["old_base_branch"]), ("main", "feat/parent"))
            self.assertIsNotNone(res["before_patch_id"])
            self.assertEqual(res["before_patch_id"], res["after_patch_id"])
            self.assertEqual((res["verdict_continuous"], res["reverify_required"], res["conflict"]), (True, False, False))
            self.assertEqual((res["pushed"], res["retargeted"]), (True, True))
            new_head = res["new_head_sha"]
            self.assertEqual(fx.origin_ref("feat/child"), new_head)
            self.assertEqual(_git(fx.origin, "rev-parse", f"{new_head}^"), new_main)
            self.assertEqual(_git(fx.origin, "rev-list", "--count", f"{new_main}..{new_head}"), "1")
            self.assertEqual(gh.edits(), [["pr", "edit", "7", "--repo", "acme/sample", "--base", "main"]])
            self._assert_clean(fx, before, tmp_before)

            row = _trail_rows("restack_pr")[0]
            self.assertEqual((row["result"], row["phase"], row["tenant"], row["card_id"]), ("continuous", "gate", "acme-sample-t1", "t_orch"))
            self.assertEqual(row["evidence"]["after_patch_id"], res["after_patch_id"])

    def test_semantic_context_change_requires_reverify(self):
        from factonio.repos import restack_pr

        with isolated_hermes_home() as home, _git_env():
            fx = StackFixture(home)
            _register(fx.local)
            fx.squash_merge_parent(main_edit=(3, "main line 3"))
            before, tmp_before = _checkout_state(fx.local), _restack_dirs()
            with mock.patch("factonio.repos._run_gh", _child_gh(fx)):
                res = restack_pr("acme/sample", 7, old_base_sha=fx.parent_tip)
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["old_base_source"], "old_base_sha")
            self.assertNotEqual(res["before_patch_id"], res["after_patch_id"])
            self.assertEqual((res["verdict_continuous"], res["reverify_required"], res["pushed"]), (False, True, True))
            self.assertEqual(_trail_rows("restack_pr")[0]["result"], "reverify_required")
            self._assert_clean(fx, before, tmp_before)

    def test_conflict_aborts_cleanly_without_push_or_retarget(self):
        from factonio.repos import restack_pr

        with isolated_hermes_home() as home, _git_env():
            fx = StackFixture(home)
            _register(fx.local)
            fx.squash_merge_parent(main_edit=(5, "main line 5"))
            before, tmp_before = _checkout_state(fx.local), _restack_dirs()
            gh = _child_gh(fx)
            with mock.patch("factonio.repos._run_gh", gh):
                res = restack_pr("acme/sample", 7, parent_pr=5)
            self.assertFalse(res["ok"])
            self.assertEqual((res["conflict"], res["verdict_continuous"], res["reverify_required"]), (True, False, True))
            self.assertEqual(res["conflict_files"], ["a.txt"])
            self.assertIn("conflict", res["error"])
            self.assertEqual((res["pushed"], res["retargeted"]), (False, False))
            self.assertEqual(fx.origin_ref("feat/child"), fx.child_sha)
            self.assertEqual(gh.edits(), [])
            self.assertEqual(_trail_rows("restack_pr")[0]["result"], "conflict")
            self._assert_clean(fx, before, tmp_before)

    def test_dry_run_and_no_push_flags(self):
        from factonio.repos import restack_pr

        with isolated_hermes_home() as home, _git_env():
            fx = StackFixture(home)
            _register(fx.local)
            fx.squash_merge_parent()
            before, tmp_before = _checkout_state(fx.local), _restack_dirs()
            gh = _child_gh(fx)
            with mock.patch("factonio.repos._run_gh", gh):
                dry = restack_pr("acme/sample", 7, parent_pr=5, dry_run=True)
                no_push = restack_pr("acme/sample", 7, parent_pr=5, push=False, retarget=False)
            for res in (dry, no_push):
                self.assertTrue(res["ok"], res)
                self.assertTrue(res["verdict_continuous"])
                self.assertEqual((res["pushed"], res["retargeted"]), (False, False))
                self.assertNotEqual(res["new_head_sha"], fx.child_sha)
            self.assertTrue(dry["dry_run"])
            self.assertEqual(fx.origin_ref("feat/child"), fx.child_sha)
            self.assertEqual(gh.edits(), [])
            self._assert_clean(fx, before, tmp_before)

    def test_refuses_closed_pr_and_stale_lease(self):
        from factonio.repos import restack_pr

        with isolated_hermes_home() as home, _git_env():
            fx = StackFixture(home)
            _register(fx.local)
            fx.squash_merge_parent()
            closed = StackGh({7: _pr_node(7, "feat/child", fx.child_sha, "feat/parent", state="CLOSED")})
            with mock.patch("factonio.repos._run_gh", closed):
                res = restack_pr("acme/sample", 7, old_base_sha=fx.parent_tip)
            self.assertFalse(res["ok"])
            self.assertIn("only OPEN", res["error"])

            # Someone pushed to the child after the gate read: --force-with-lease must refuse.
            _git(fx.seed, "checkout", "-q", "-B", "feat/child", "origin/feat/child")
            racer = _commit(fx.seed, {"r.txt": "race\n"}, "race")
            _git(fx.seed, "push", "-q", "origin", "feat/child")
            gh = _child_gh(fx)
            with mock.patch("factonio.repos._run_gh", gh):
                res = restack_pr("acme/sample", 7, old_base_sha=fx.parent_tip)
            self.assertFalse(res["ok"])
            self.assertIn("force-with-lease", res["error"])
            self.assertEqual(fx.origin_ref("feat/child"), racer)
            self.assertEqual(gh.edits(), [])
            self.assertEqual(_worktree_count(fx.local), 1)

    def test_restack_stack_restacks_every_open_child(self):
        from factonio.repos import restack_stack

        with isolated_hermes_home() as home, _git_env():
            fx = StackFixture(home)
            _register(fx.local)
            second = fx.add_child("feat/child2", {"c2.txt": "two\n"})
            fx.squash_merge_parent()
            gh = StackGh(
                {
                    7: _pr_node(7, "feat/child", fx.child_sha, "feat/parent"),
                    8: _pr_node(8, "feat/child2", second, "feat/parent"),
                },
                views={5: {"headRefOid": fx.parent_tip, "headRefName": "feat/parent", "baseRefName": "main", "state": "MERGED"}},
                children=[{"number": 8, "headRefName": "feat/child2"}, {"number": 7, "headRefName": "feat/child"}],
            )
            with mock.patch("factonio.repos._run_gh", gh):
                res = restack_stack("acme/sample", parent_pr=5)
            self.assertTrue(res["ok"], res)
            self.assertEqual((res["from_branch"], res["new_base"], res["parent_head_sha"]), ("feat/parent", "main", fx.parent_tip))
            self.assertEqual([r["pr_number"] for r in res["results"]], [7, 8])
            self.assertTrue(all(r["verdict_continuous"] and r["old_base_commit"] == fx.parent_tip for r in res["results"]))
            list_call = next(c for c in gh.calls if c[:2] == ["pr", "list"])
            self.assertEqual(list_call[list_call.index("--base") + 1], "feat/parent")
            self.assertEqual(len(gh.edits()), 2)
            with self.assertRaises(ValueError):
                restack_stack("acme/sample")


class _CliCtx:
    def __init__(self) -> None:
        self.cli: dict = {}

    def register_cli_command(self, name, *, help, setup_fn, handler_fn):
        self.cli[name] = (setup_fn, handler_fn)


class _ToolCtx:
    def __init__(self) -> None:
        self.tools: dict[str, dict[str, Any]] = {}

    def register_tool(self, *, name, toolset, schema, handler, **kw):
        self.tools[name] = {"schema": schema, "handler": handler}


def _run_cli(*argv: str) -> tuple[int, str]:
    from factonio.commands import register_cli

    ctx = _CliCtx()
    register_cli(ctx, PLUGIN_ROOT)
    setup_fn, handler_fn = ctx.cli["factonio"]
    parser = argparse.ArgumentParser(prog="hermes factonio")
    setup_fn(parser)
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            handler_fn(parser.parse_args(list(argv)))
        except SystemExit as exc:
            code = int(exc.code or 0)
    return code, out.getvalue() + err.getvalue()


@unittest.skipUnless(shutil.which("git"), "git is required")
class TestRestackCliAndTools(unittest.TestCase):
    def test_cli_restack_text_json_and_usage(self):
        with isolated_hermes_home() as home, _git_env():
            fx = StackFixture(home)
            _register(fx.local)
            fx.squash_merge_parent()
            gh = _child_gh(fx)
            with mock.patch("factonio.repos._run_gh", gh):
                code, out = _run_cli("restack", "acme/sample", "7", "--parent-pr", "5", "--dry-run")
                self.assertEqual(code, 0, out)
                self.assertIn("CONTINUOUS: acme/sample#7 feat/parent -> main", out)
                code, out = _run_cli("restack", "acme/sample", "7", "--parent-pr", "5", "--no-push", "--no-retarget", "--json")
                self.assertEqual(code, 0, out)
                data = json.loads(out)
                self.assertEqual((data["verdict_continuous"], data["pushed"], data["retargeted"]), (True, False, False))
            self.assertEqual(fx.origin_ref("feat/child"), fx.child_sha)
            code, out = _run_cli("restack", "acme/sample")
            self.assertEqual(code, 1)
            self.assertIn("pr_number, --parent-pr, or --from-branch", out)

    def test_tools_registered_and_handlers_route(self):
        import hermes_yaml as yaml
        from factonio.agent_tools import register_tools

        ctx = _ToolCtx()
        register_tools(ctx)
        manifest = yaml.safe_load((PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8"))
        self.assertIn("factonio_restack_pr", manifest["provides_tools"])
        self.assertEqual(sorted(manifest["provides_tools"]), sorted(ctx.tools))
        props = ctx.tools["factonio_spawn_implement"]["schema"]["parameters"]["properties"]
        self.assertIn("base_branch", props)
        self.assertEqual(props["stack_parent_pr"]["type"], "integer")
        rs_props = ctx.tools["factonio_restack_pr"]["schema"]["parameters"]["properties"]
        self.assertTrue({"pr_number", "parent_pr", "from_branch", "new_base", "old_base_sha", "dry_run"} <= set(rs_props))

        handler = ctx.tools["factonio_restack_pr"]["handler"]
        with mock.patch("factonio.agent_tools.restack_pr", return_value={"ok": True, "pr_number": 7}) as one, \
                mock.patch("factonio.agent_tools.restack_stack", return_value={"ok": True, "results": []}) as many:
            self.assertTrue(json.loads(handler({"repo": "acme/sample", "pr_number": 7, "dry_run": True, "tenant": "t1"}))["ok"])
            self.assertTrue(json.loads(handler({"repo": "acme/sample", "parent_pr": 5}))["ok"])
        self.assertEqual(one.call_args.args, ("acme/sample", 7))
        self.assertEqual((one.call_args.kwargs["dry_run"], one.call_args.kwargs["tenant"]), (True, "t1"))
        self.assertEqual(many.call_args.kwargs["parent_pr"], 5)

        with isolated_hermes_home() as home, _git_env():
            fx = StackFixture(home)
            _register(fx.local)
            spawn = ctx.tools["factonio_spawn_implement"]["handler"]
            bad = json.loads(spawn({"repo": "acme/sample", "tenant": "t", "title": "x", "body": "y", "stack_parent_pr": -1}))
            self.assertFalse(bad["ok"])
            good = json.loads(spawn({
                "repo": "acme/sample", "tenant": "t", "title": "x", "body": "y",
                "base_branch": "feat/parent", "stack_parent_pr": 5,
            }))
            self.assertEqual((good["is_stacked"], good["branch_seeded"], good["stack_parent_pr"]), (True, True, 5))


if __name__ == "__main__":
    unittest.main()
