"""Clean-room tests for `audit_worktrees`, `prune_safe_worktrees`, and `hermes factonio worktrees`."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any
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

PRS = [
    {"number": 7, "state": "OPEN", "isDraft": False, "headRefName": "feat-pr", "url": "https://x/7"},
    {"number": 8, "state": "CLOSED", "isDraft": False, "headRefName": "feat-closed", "url": "https://x/8"},
    {"number": 9, "state": "OPEN", "isDraft": True, "headRefName": "feat-draft", "url": "https://x/9"},
    {"number": 3, "state": "CLOSED", "isDraft": False, "headRefName": "feat-draft", "url": "https://x/3"},
    {"number": 10, "state": "MERGED", "isDraft": False, "headRefName": "feat-merged", "url": "https://x/10"},
]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True).stdout


def _commit(cwd: Path, name: str, content: str = "x") -> None:
    (cwd / name).write_text(content, encoding="utf-8")
    _git(cwd, "add", name)
    _git(cwd, "commit", "-qm", f"add {name}")


def _gh_ok(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
    assert args[:2] == ["pr", "list"], args
    return subprocess.CompletedProcess(["gh", *args], 0, json.dumps(PRS), "")


def _build_fixture(home: Path) -> dict[str, Path]:
    """Main checkout with origin + one linked worktree per safety bucket."""
    origin = home / "origin.git"
    _git(home, "init", "-q", "--bare", "-b", "main", str(origin))
    main = home / "dev" / "acme-api"
    main.parent.mkdir(parents=True)
    _git(home, "clone", "-q", str(origin), str(main))
    _git(main, "symbolic-ref", "HEAD", "refs/heads/main")
    _commit(main, "README.md", "hello")
    _git(main, "push", "-q", "-u", "origin", "main")
    _git(main, "remote", "set-url", "origin", "https://github.com/acme/api.git")
    # Keep a working fetch URL for the push below while gh lookups see acme/api.
    _git(main, "remote", "set-url", "--push", "origin", str(origin))

    wts = home / "wt"
    paths: dict[str, Path] = {}

    def add(name: str, branch: str | None) -> Path:
        path = wts / name
        if branch:
            _git(main, "worktree", "add", "-q", "-b", branch, str(path), "main")
        else:
            _git(main, "worktree", "add", "-q", "--detach", str(path), "main")
        paths[name] = path
        return path

    # safe: commits merged into main (fast-forward) and pushed.
    p = add("merged", "feat-merged")
    _commit(p, "merged.txt", "m" * 4096)
    _git(main, "merge", "-q", "--ff-only", "feat-merged")
    _git(p, "push", "-q", "origin", "feat-merged")
    _git(main, "fetch", "-q", str(origin), "+refs/heads/*:refs/remotes/origin/*")
    _git(p, "branch", "-q", "--set-upstream-to=origin/feat-merged")
    _git(main, "push", "-q", "origin", "main")
    _git(main, "fetch", "-q", str(origin), "+refs/heads/*:refs/remotes/origin/*")

    # safe: no commits, only regenerable scratch output.
    p = add("scratch", "feat-scratch")
    (p / "__pycache__").mkdir()
    (p / "__pycache__" / "mod.cpython.pyc").write_bytes(b"\0" * 2048)

    # safe: unmerged commit but its PR was closed.
    p = add("closed", "feat-closed")
    _commit(p, "closed.txt")

    # hold-wip: merged base but an uncommitted tracked edit (plus an untracked file).
    p = add("wip", "feat-wip")
    (p / "README.md").write_text("edited", encoding="utf-8")
    (p / "new_module.py").write_text("x = 1\n", encoding="utf-8")

    # hold-active-card: merged, clean, but a running card owns the workspace.
    add("card", "feat-card")

    # hold-active-card by branch name (workspace path differs).
    add("card-branch", "feat-card-branch")

    # hold-open-pr / draft.
    p = add("pr", "feat-pr")
    _commit(p, "pr.txt")
    p = add("draft", "feat-draft")
    _commit(p, "draft.txt")

    # review: unmerged local commit, no PR, never pushed.
    p = add("review", "feat-review")
    _commit(p, "review.txt")

    # review: detached HEAD with an orphan commit.
    p = add("detached", None)
    _commit(p, "orphan.txt")

    # done card must not hold its worktree.
    add("donecard", "feat-donecard")

    paths["main"] = main
    paths["origin"] = origin
    return paths


def _seed_cards(db_path: Path, paths: dict[str, Path]) -> None:
    from hermes_cli.kanban_db_connect import connect_closing

    with connect_closing(db_path=db_path) as conn:
        for tid, status, ws, branch in (
            ("t_card", "running", str(paths["card"]), None),
            ("t_card_branch", "blocked", "/elsewhere/not-here", "feat-card-branch"),
            ("t_done", "done", str(paths["donecard"]), "feat-donecard"),
            ("t_arch", "archived", str(paths["merged"]), "feat-merged"),
        ):
            conn.execute(
                "INSERT INTO tasks (id, title, assignee, status, workspace_path, branch_name, created_at)"
                " VALUES (?, ?, 'factonio', ?, ?, ?, 0)",
                (tid, tid, status, ws, branch),
            )
        conn.commit()


class _FakeCtx:
    def __init__(self) -> None:
        self.cli: dict[str, Any] = {}

    def register_cli_command(self, name: str, *, help: str, setup_fn: Any, handler_fn: Any) -> None:
        self.cli[name] = {"setup_fn": setup_fn, "handler_fn": handler_fn}


def _cli():
    from factonio.commands import register_cli

    ctx = _FakeCtx()
    register_cli(ctx, PLUGIN_ROOT)
    entry = ctx.cli["factonio"]
    parser = argparse.ArgumentParser(prog="hermes factonio")
    entry["setup_fn"](parser)

    def run(*argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                entry["handler_fn"](parser.parse_args(list(argv)))
            except SystemExit as exc:
                code = int(exc.code or 0)
        return code, out.getvalue(), err.getvalue()

    return run


def _exclude(main: Path, *patterns: str) -> None:
    """Add ignore patterns via the shared ``info/exclude`` (applies to every linked worktree)."""
    common = Path(_git(main, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    (common / "info").mkdir(exist_ok=True)
    with (common / "info" / "exclude").open("a", encoding="utf-8") as fh:
        fh.write("".join(f"{p}\n" for p in patterns))


def _worktree_paths(main: Path) -> set[str]:
    out = _git(main, "worktree", "list", "--porcelain")
    return {line[len("worktree "):] for line in out.splitlines() if line.startswith("worktree ")}


class TestWorktreeAudit(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, GIT_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _audit(self, paths, **kw):
        from factonio import repos

        with mock.patch.object(repos, "_run_gh", side_effect=_gh_ok):
            return repos.audit_worktrees(str(paths["main"]), **kw)

    def test_porcelain_parser(self):
        from factonio.repos import _parse_worktree_porcelain

        text = (
            "worktree /r\nHEAD aaa\nbranch refs/heads/main\n\n"
            "worktree /r/wt1\nHEAD bbb\nbranch refs/heads/feat\nlocked reason here\n\n"
            "worktree /r/wt2\nHEAD ccc\ndetached\nprunable gitdir file points to non-existent location\n"
        )
        entries = _parse_worktree_porcelain(text)
        self.assertEqual([e["path"] for e in entries], ["/r", "/r/wt1", "/r/wt2"])
        self.assertEqual(entries[1]["branch"], "feat")
        self.assertEqual(entries[1]["locked"], "reason here")
        self.assertTrue(entries[2]["detached"])
        self.assertNotIn("branch", entries[2])

    def test_audit_classifies_every_bucket(self):
        with isolated_hermes_home() as home:
            paths = _build_fixture(home)
            _seed_cards(home / "kanban.db", paths)
            res = self._audit(paths)
            self.assertTrue(res["ok"])
            repo = res["repos"][0]
            self.assertEqual(repo["repo"], "acme/api")
            self.assertEqual(repo["base_ref"], "origin/main")
            by_name = {Path(w["path"]).name: w for w in res["worktrees"]}
            self.assertNotIn(paths["main"].name, by_name)
            self.assertEqual(len(by_name), 11)

            expected = {
                "merged": "safe", "scratch": "safe", "closed": "safe",
                "wip": "hold-wip", "card": "hold-active-card", "card-branch": "hold-active-card",
                "pr": "hold-open-pr", "draft": "hold-open-pr", "review": "review", "detached": "review",
                "donecard": "safe",
            }
            for name, bucket in expected.items():
                self.assertEqual(by_name[name]["bucket"], bucket, f"{name}: {by_name[name]}")

            m = by_name["merged"]
            self.assertTrue(m["merged"])
            self.assertEqual((m["dirty"], m["remote"], m["pr"]), ("clean", "pushed", "#10/merged"))
            self.assertEqual(m["branch"], "feat-merged")
            self.assertEqual(len(m["head_sha"]), 40)
            self.assertGreater(m["size_bytes"], 4096)
            self.assertTrue(m["size_human"].endswith(("KiB", "MiB")))
            self.assertIsInstance(m["age_days"], float)
            self.assertIsNone(m["active_card"])  # archived card does not count

            self.assertEqual(by_name["scratch"]["dirty"], "scratch:1")
            self.assertEqual(by_name["wip"]["dirty"], "wip:1")
            self.assertIn("wip:1", by_name["wip"]["hold_reason"])
            self.assertIn("1 tracked edit", by_name["wip"]["hold_reason"])
            self.assertEqual(by_name["card"]["active_card"], "t_card")
            self.assertEqual(by_name["card-branch"]["active_card"], "t_card_branch")
            self.assertEqual(by_name["pr"]["pr"], "#7/open")
            self.assertEqual(by_name["draft"]["pr"], "#9/draft")
            self.assertEqual(by_name["closed"]["pr"], "#8/closed")
            self.assertFalse(by_name["closed"]["merged"])
            r = by_name["review"]
            self.assertEqual((r["merged"], r["remote"], r["pr"]), (False, "no-remote", "-"))
            self.assertEqual(by_name["detached"]["remote"], "detached")
            self.assertIsNone(by_name["detached"]["branch"])

            counts = res["summary"]["by_bucket"]
            self.assertEqual(counts, {"hold-wip": 1, "hold-active-card": 2, "hold-open-pr": 2, "safe": 4, "review": 2})

    def test_wip_beats_active_card_beats_open_pr(self):
        with isolated_hermes_home() as home:
            paths = _build_fixture(home)
            _seed_cards(home / "kanban.db", paths)
            (paths["card"] / "README.md").write_text("edited\n", encoding="utf-8")
            from hermes_cli.kanban_db_connect import connect_closing

            with connect_closing(db_path=home / "kanban.db") as conn:
                conn.execute(
                    "INSERT INTO tasks (id, title, assignee, status, workspace_path, created_at)"
                    " VALUES ('t_pr', 't', 'factonio', 'ready', ?, 0)",
                    (str(paths["pr"]),),
                )
                conn.commit()
            by_name = {Path(w["path"]).name: w for w in self._audit(paths)["worktrees"]}
            self.assertEqual(by_name["card"]["bucket"], "hold-wip")
            self.assertEqual(by_name["pr"]["bucket"], "hold-active-card")

    def test_ahead_remote_and_base_override(self):
        with isolated_hermes_home() as home:
            paths = _build_fixture(home)
            _git(paths["merged"], "commit", "-q", "--allow-empty", "-m", "local only")
            res = self._audit(paths, base_ref="main", check_pr=False)
            by_name = {Path(w["path"]).name: w for w in res["worktrees"]}
            self.assertEqual(res["repos"][0]["base_ref"], "main")
            self.assertEqual(by_name["merged"]["remote"], "ahead:1")
            self.assertFalse(by_name["merged"]["merged"])
            self.assertEqual(by_name["merged"]["bucket"], "review")
            self.assertEqual(by_name["pr"]["pr"], "-")
            self.assertEqual(by_name["pr"]["bucket"], "review")
            with self.assertRaises(ValueError):
                self._audit(paths, base_ref="no-such-ref")

    def test_gh_failure_degrades_gracefully(self):
        from factonio import repos

        with isolated_hermes_home() as home:
            paths = _build_fixture(home)
            fail = subprocess.CompletedProcess(["gh"], 1, "", "gh: not authenticated")
            with mock.patch.object(repos, "_run_gh", return_value=fail):
                res = repos.audit_worktrees(str(paths["main"]))
            self.assertIn("not authenticated", res["repos"][0]["pr_error"])
            by_name = {Path(w["path"]).name: w for w in res["worktrees"]}
            self.assertEqual(by_name["closed"]["bucket"], "review")
            self.assertEqual(by_name["merged"]["bucket"], "safe")

    def test_audit_is_read_only(self):
        with isolated_hermes_home() as home:
            paths = _build_fixture(home)
            before = _worktree_paths(paths["main"])
            self._audit(paths)
            self.assertEqual(_worktree_paths(paths["main"]), before)
            self.assertTrue((paths["scratch"] / "__pycache__").is_dir())

    def test_audit_does_not_rewrite_worktree_index(self):
        with isolated_hermes_home() as home:
            paths = _build_fixture(home)
            wt = paths["merged"]
            index = Path(_git(wt, "rev-parse", "--absolute-git-dir").strip()) / "index"
            # Stat-only change to a tracked file: plain `git status` would refresh and rewrite the index.
            tracked = wt / "merged.txt"
            st = tracked.stat()
            os.utime(tracked, ns=(st.st_atime_ns, st.st_mtime_ns - 3_600_000_000_000))
            old = index.stat().st_mtime_ns - 7_200_000_000_000
            os.utime(index, ns=(old, old))
            before = (index.read_bytes(), index.stat().st_mtime_ns)
            res = self._audit(paths, check_pr=False)
            by_name = {Path(w["path"]).name: w for w in res["worktrees"]}
            self.assertEqual(by_name["merged"]["dirty"], "clean")
            self.assertEqual((index.read_bytes(), index.stat().st_mtime_ns), before)

    def _prune_with_post_audit_hook(self, paths, hook):
        from factonio import repos

        real_audit = repos.audit_worktrees

        def audit_then_hook(*args: Any, **kw: Any) -> dict[str, Any]:
            out = real_audit(*args, **kw)
            hook()
            return out

        with mock.patch.object(repos, "_run_gh", side_effect=_gh_ok), \
                mock.patch.object(repos, "audit_worktrees", side_effect=audit_then_hook):
            return repos.prune_safe_worktrees(str(paths["main"]))

    def test_prune_holds_worktree_claimed_by_active_card_after_audit(self):
        from hermes_cli.kanban_db_connect import connect_closing

        with isolated_hermes_home() as home:
            paths = _build_fixture(home)
            db = home / "kanban.db"
            _seed_cards(db, paths)
            self.assertEqual(
                {Path(w["path"]).name: w for w in self._audit(paths)["worktrees"]}["merged"]["bucket"], "safe"
            )

            def claim() -> None:
                with connect_closing(db_path=db) as conn:
                    conn.execute(
                        "INSERT INTO tasks (id, title, assignee, status, workspace_path, branch_name, created_at)"
                        " VALUES ('t_late', 't_late', 'factonio', 'running', ?, NULL, 0)",
                        (str(paths["merged"]),),
                    )
                    conn.commit()

            res = self._prune_with_post_audit_hook(paths, claim)
            removed = {Path(r["path"]).name for r in res["removed"]}
            held = {Path(h["path"]).name: h for h in res["held"]}
            self.assertNotIn("merged", removed)
            self.assertTrue(paths["merged"].is_dir())
            self.assertIn(str(paths["merged"]), _worktree_paths(paths["main"]))
            self.assertEqual(held["merged"]["bucket"], "hold-active-card")
            self.assertIn("t_late", held["merged"]["hold_reason"])
            # Unclaimed safe worktrees are still pruned.
            self.assertIn("scratch", removed)

    def test_prune_fails_closed_when_kanban_db_unreadable_before_removal(self):
        with isolated_hermes_home() as home:
            paths = _build_fixture(home)
            db = home / "kanban.db"
            _seed_cards(db, paths)

            def corrupt() -> None:
                for suffix in ("-wal", "-shm"):
                    Path(f"{db}{suffix}").unlink(missing_ok=True)
                db.write_bytes(b"this is not a sqlite database" * 64)

            res = self._prune_with_post_audit_hook(paths, corrupt)
            self.assertEqual(res["removed"], [])
            self.assertFalse(res["ok"])
            held = {Path(h["path"]).name: h for h in res["held"]}
            for name in ("merged", "scratch", "closed", "donecard"):
                self.assertTrue(paths[name].is_dir(), name)
                self.assertEqual(held[name]["bucket"], "hold-active-card", held[name])
                self.assertIn("could not be verified", held[name]["hold_reason"])

    def _assert_nothing_removed(self, res, paths, names=("merged", "scratch", "closed", "donecard")):
        self.assertFalse(res["ok"], res)
        self.assertEqual(res["removed"], [])
        self.assertTrue(res["kanban_errors"], res)
        held = {Path(h["path"]).name: h for h in res["held"]}
        for name in names:
            self.assertTrue(paths[name].is_dir(), name)
            self.assertIn(str(paths[name]), _worktree_paths(paths["main"]))
            self.assertEqual(held[name]["bucket"], "hold-active-card", held[name])
            self.assertIn("could not be verified", held[name]["hold_reason"])
        self.assertEqual((paths["merged"] / "merged.txt").read_text(encoding="utf-8"), "m" * 4096)

    def test_prune_fails_closed_when_kanban_db_disappears_after_audit(self):
        with isolated_hermes_home() as home:
            paths = _build_fixture(home)
            db = home / "kanban.db"
            _seed_cards(db, paths)

            def vanish() -> None:
                for suffix in ("-wal", "-shm"):
                    Path(f"{db}{suffix}").unlink(missing_ok=True)
                db.rename(home / "kanban.db.moved")

            res = self._prune_with_post_audit_hook(paths, vanish)
            self._assert_nothing_removed(res, paths)

    def test_prune_fails_closed_when_explicit_kanban_db_path_unavailable(self):
        from factonio import repos

        with isolated_hermes_home() as home:
            paths = _build_fixture(home)
            (home / "a-directory.db").mkdir()
            for bad in (home / "no-such.db", home / "a-directory.db"):
                with mock.patch.object(repos, "_run_gh", side_effect=_gh_ok):
                    res = repos.prune_safe_worktrees(str(paths["main"]), kanban_db_path=bad)
                self._assert_nothing_removed(res, paths)

    def test_prune_holds_worktree_locked_after_audit(self):
        with isolated_hermes_home() as home:
            paths = _build_fixture(home)
            _seed_cards(home / "kanban.db", paths)
            def hook() -> None:
                _git(paths["main"], "worktree", "lock", "--reason", "operator hold", str(paths["merged"]))

            res = self._prune_with_post_audit_hook(paths, hook)
            removed = {Path(r["path"]).name for r in res["removed"]}
            held = {Path(h["path"]).name: h for h in res["held"]}
            self.assertNotIn("merged", removed)
            self.assertEqual(held["merged"]["bucket"], "review")
            self.assertIn("locked", held["merged"]["hold_reason"])
            self.assertEqual((paths["merged"] / "merged.txt").read_text(encoding="utf-8"), "m" * 4096)
            self.assertIn(str(paths["merged"]), _worktree_paths(paths["main"]))
            self.assertIn("scratch", removed)

    def test_prune_never_rmtrees_worktree_locked_during_git_remove(self):
        from factonio import repos

        with isolated_hermes_home() as home:
            paths = _build_fixture(home)
            real_run_git = repos._run_git

            def lock_then_remove(args: list[str], **kw: Any):
                if args[:2] == ["worktree", "remove"] and Path(args[-1]) == paths["merged"]:
                    _git(paths["main"], "worktree", "lock", "--reason", "operator hold", str(paths["merged"]))
                return real_run_git(args, **kw)

            with mock.patch.object(repos, "_run_gh", side_effect=_gh_ok), \
                    mock.patch.object(repos, "_run_git", side_effect=lock_then_remove):
                res = repos.prune_safe_worktrees(str(paths["main"]))
            removed = {Path(r["path"]).name for r in res["removed"]}
            held = {Path(h["path"]).name: h for h in res["held"]}
            self.assertNotIn("merged", removed)
            self.assertEqual(held["merged"]["bucket"], "review")
            self.assertIn("locked", held["merged"]["hold_reason"])
            self.assertEqual((paths["merged"] / "merged.txt").read_text(encoding="utf-8"), "m" * 4096)
            self.assertIn(str(paths["merged"]), _worktree_paths(paths["main"]))

    def test_prune_rmtree_fallback_only_for_deregistered_leftovers(self):
        from factonio import repos

        with isolated_hermes_home() as home:
            paths = _build_fixture(home)
            real_run_git = repos._run_git
            target = paths["scratch"]

            def remove_leaving_artifacts(args: list[str], **kw: Any):
                proc = real_run_git(args, **kw)
                if args[:2] == ["worktree", "remove"] and Path(args[-1]) == target:
                    # git de-registered the worktree but could not delete a leftover directory.
                    (target / "build" / "stuck").mkdir(parents=True)
                    (target / ".git").write_text(f"gitdir: {paths['main']}/.git/worktrees/scratch\n", encoding="utf-8")
                    return subprocess.CompletedProcess(proc.args, 1, "", "error: failed to delete: Directory not empty")
                return proc

            with mock.patch.object(repos, "_run_gh", side_effect=_gh_ok), \
                    mock.patch.object(repos, "_run_git", side_effect=remove_leaving_artifacts):
                res = repos.prune_safe_worktrees(str(paths["main"]))
            removed = {Path(r["path"]).name: r for r in res["removed"]}
            self.assertEqual(removed["scratch"]["method"], "fallback rmtree")
            self.assertFalse(target.exists())
            self.assertTrue(res["ok"], res)

    def test_prune_holds_worktree_whose_head_moved_after_audit(self):
        with isolated_hermes_home() as home:
            paths = _build_fixture(home)
            res = self._prune_with_post_audit_hook(paths, lambda: _commit(paths["merged"], "late.txt"))
            held = {Path(h["path"]).name: h for h in res["held"]}
            self.assertTrue((paths["merged"] / "late.txt").is_file())
            self.assertEqual(held["merged"]["bucket"], "review")
            self.assertIn("changed since audit", held["merged"]["hold_reason"])

    def test_prune_safe_only_removes_safe(self):
        from factonio import repos

        with isolated_hermes_home() as home:
            paths = _build_fixture(home)
            _seed_cards(home / "kanban.db", paths)
            audit = self._audit(paths)
            safe_bytes = audit["summary"]["safe_bytes"]
            with mock.patch.object(repos, "_run_gh", side_effect=_gh_ok):
                res = repos.prune_safe_worktrees(str(paths["main"]))
            self.assertTrue(res["ok"], res)
            removed = {Path(r["path"]).name for r in res["removed"]}
            self.assertEqual(removed, {"merged", "scratch", "closed", "donecard"})
            held = {Path(h["path"]).name: h for h in res["held"]}
            self.assertEqual(set(held), {"wip", "card", "card-branch", "pr", "draft", "review", "detached"})
            self.assertTrue(all(h["hold_reason"] for h in held.values()))
            self.assertEqual(res["reclaimed_bytes"], safe_bytes)
            self.assertGreater(res["reclaimed_bytes"], 4096)

            remaining = _worktree_paths(paths["main"])
            for name in removed:
                self.assertFalse(paths[name].exists(), name)
                self.assertNotIn(str(paths[name]), remaining)
            for name in held:
                self.assertTrue(paths[name].is_dir(), name)
            self.assertEqual((paths["wip"] / "README.md").read_text(encoding="utf-8"), "edited")
            # Branch refs survive; only the checkout is removed.
            self.assertIn("feat-closed", _git(paths["main"], "branch", "--list", "feat-closed"))

    def test_prune_rechecks_dirty_state_before_removal(self):
        from factonio import repos

        with isolated_hermes_home() as home:
            paths = _build_fixture(home)
            _exclude(paths["main"], "local-data/")
            real_inventory = repos._dirty_inventory
            calls: dict[str, int] = {}

            def late_work(wt_path: Path) -> dict[str, Any]:
                calls[wt_path.name] = calls.get(wt_path.name, 0) + 1
                if calls[wt_path.name] == 2:
                    if wt_path.name == "merged":
                        (wt_path / "merged.txt").write_text("late edit", encoding="utf-8")
                    elif wt_path.name == "closed":
                        (wt_path / "local-data").mkdir()
                        (wt_path / "local-data" / "late.db").write_bytes(b"unique")
                return real_inventory(wt_path)

            with mock.patch.object(repos, "_run_gh", side_effect=_gh_ok), \
                    mock.patch.object(repos, "_dirty_inventory", side_effect=late_work):
                res = repos.prune_safe_worktrees(str(paths["main"]))
            held = {Path(h["path"]).name: h for h in res["held"]}
            self.assertTrue(paths["merged"].is_dir())
            self.assertEqual(held["merged"]["bucket"], "hold-wip")
            self.assertIn("tracked edit", held["merged"]["hold_reason"])
            self.assertEqual((paths["closed"] / "local-data" / "late.db").read_bytes(), b"unique")
            self.assertEqual(held["closed"]["bucket"], "hold-wip")
            self.assertIn("ignored local data", held["closed"]["hold_reason"])

    def test_ignored_local_data_holds_worktree_and_survives_prune(self):
        from factonio import repos

        with isolated_hermes_home() as home:
            paths = _build_fixture(home)
            _exclude(paths["main"], "local-data/", ".env", "__pycache__/", "*.pyc", "node_modules/")
            (paths["merged"] / "local-data").mkdir()
            (paths["merged"] / "local-data" / "important.json").write_text('{"k": 1}', encoding="utf-8")
            (paths["closed"] / ".env").write_text("TOKEN=local\n", encoding="utf-8")

            by_name = {Path(w["path"]).name: w for w in self._audit(paths)["worktrees"]}
            for name in ("merged", "closed"):
                self.assertEqual(by_name[name]["dirty"], "wip:1", by_name[name])
                self.assertEqual(by_name[name]["bucket"], "hold-wip")
                self.assertIn("ignored local data", by_name[name]["hold_reason"])
            self.assertIn("local-data/", by_name["merged"]["hold_reason"])
            # Ignored regenerable caches never block safety.
            self.assertEqual(by_name["scratch"]["dirty"], "clean")
            self.assertEqual(by_name["scratch"]["bucket"], "safe")

            with mock.patch.object(repos, "_run_gh", side_effect=_gh_ok):
                res = repos.prune_safe_worktrees(str(paths["main"]))
            removed = {Path(r["path"]).name for r in res["removed"]}
            self.assertNotIn("merged", removed)
            self.assertNotIn("closed", removed)
            self.assertIn("scratch", removed)
            self.assertEqual(
                (paths["merged"] / "local-data" / "important.json").read_text(encoding="utf-8"), '{"k": 1}'
            )
            self.assertEqual((paths["closed"] / ".env").read_text(encoding="utf-8"), "TOKEN=local\n")

    def test_scratch_only_merged_worktree_is_safe_and_pruned(self):
        from factonio import repos

        with isolated_hermes_home() as home:
            paths = _build_fixture(home)
            _exclude(paths["main"], "__pycache__/", "*.pyc")
            (paths["merged"] / "notes.txt").write_text("n" * 8192, encoding="utf-8")
            (paths["merged"] / "pkg").mkdir()
            (paths["merged"] / "pkg" / "__pycache__").mkdir()
            (paths["merged"] / "pkg" / "__pycache__" / "foo.cpython.pyc").write_bytes(b"\0" * 512)

            by_name = {Path(w["path"]).name: w for w in self._audit(paths)["worktrees"]}
            self.assertEqual(by_name["merged"]["dirty"], "scratch:1")
            self.assertEqual(by_name["merged"]["bucket"], "safe")

            with mock.patch.object(repos, "_run_gh", side_effect=_gh_ok):
                res = repos.prune_safe_worktrees(str(paths["main"]))
            removed = {Path(r["path"]).name: r for r in res["removed"]}
            self.assertIn("merged", removed)
            self.assertEqual(removed["merged"]["dropped_scratch"], ["notes.txt"])
            self.assertFalse(paths["merged"].exists())
            self.assertNotIn(str(paths["merged"]), _worktree_paths(paths["main"]))
            self.assertGreater(res["reclaimed_bytes"], 8192)

    def test_parse_status_z_handles_renames_and_spaces(self):
        from factonio.repos import parse_status_z

        raw = "R  new name.py\0old name.py\0 M a.txt\0?? dir/x y.txt\0!! .env\0"
        self.assertEqual(
            parse_status_z(raw),
            [("R ", "new name.py"), (" M", "a.txt"), ("??", "dir/x y.txt"), ("!!", ".env")],
        )

    def test_unknown_repo_spec_raises(self):
        from factonio.repos import RepoNotFoundError, audit_worktrees

        with isolated_hermes_home() as home:
            with self.assertRaises(RepoNotFoundError):
                audit_worktrees(str(home / "missing-dir"))


class TestWorktreesCli(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, GIT_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_cli_read_only_json_text_and_prune(self):
        from factonio import repos

        with isolated_hermes_home() as home:
            paths = _build_fixture(home)
            _seed_cards(home / "kanban.db", paths)
            repos.add_repo("acme/api", local_path=str(paths["main"]), clone=False, inspect_remote=False)
            run = _cli()
            before = _worktree_paths(paths["main"])
            with mock.patch.object(repos, "_run_gh", side_effect=_gh_ok):
                code, out, _ = run("worktrees", "--json")
                self.assertEqual(code, 0)
                data = json.loads(out)
                self.assertEqual(data["summary"]["by_bucket"]["safe"], 4)
                self.assertEqual(_worktree_paths(paths["main"]), before)

                code, out, _ = run("worktrees", "acme/api", "--base", "origin/main")
                self.assertEqual(code, 0)
                self.assertIn("hold-wip", out)
                self.assertIn("Reclaimable with --prune-safe", out)
                self.assertEqual(_worktree_paths(paths["main"]), before)

                code, out, _ = run("worktrees", "acme-api", "--prune-safe")
                self.assertEqual(code, 0)
                self.assertIn("Reclaimed", out)
                self.assertIn("Held", out)
            self.assertEqual(len(before) - len(_worktree_paths(paths["main"])), 4)

            code, out, _ = run("--json", "worktrees", "nope/missing")
            self.assertEqual(code, 1)
            self.assertFalse(json.loads(out)["ok"])

            code, out, _ = run()
            self.assertIn("worktrees", out)


if __name__ == "__main__":
    unittest.main()
