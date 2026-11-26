"""Clean-room tests for publish_policy: ask local-only delivery and local-branch verification targets (Unit 2)."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
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

SHA = "ab" * 20
SHA256 = "cd" * 32
TWO_VENDORS = ["anthropic/claude-opus-4-6", "openai/gpt-5"]


class _FakeCtx:
    def __init__(self) -> None:
        self.cli: dict[str, Any] = {}
        self.tools: dict[str, dict[str, Any]] = {}

    def register_cli_command(self, name: str, *, help: str, setup_fn: Any, handler_fn: Any) -> None:
        self.cli[name] = {"help": help, "setup_fn": setup_fn, "handler_fn": handler_fn}

    def register_tool(self, *, name: str, toolset: str, schema: dict, handler: Any) -> None:
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler}


def _tools() -> dict[str, dict[str, Any]]:
    from factonio.agent_tools import register_tools

    ctx = _FakeCtx()
    register_tools(ctx)
    return ctx.tools


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


def _register(home: Path, spec: str = "acme/svc", *, git_repo: bool = False, **kw: Any) -> Path:
    from factonio.repos import add_repo

    if git_repo:
        from tests.test_verify_harness import _make_repo

        local = _make_repo(home / "dev", spec.split("/")[1])
    else:
        local = home / "dev" / spec.split("/")[1]
        (local / ".git").mkdir(parents=True, exist_ok=True)
    add_repo(spec, local_path=str(local), clone=False, inspect_remote=False, **kw)
    return local


def _set_repo_field(slug: str, **fields: Any) -> None:
    from factonio.config import list_repos, save_repos_store

    repos = list_repos()
    repos[slug].update(fields)
    save_repos_store(repos)


def _get_task(task_id: str):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing

    with connect_closing() as conn:
        task = kb.get_task(conn, task_id)
    assert task is not None
    return task


def _complete(task_id: str, verdict: Optional[str], **meta: Any) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing

    metadata = dict(meta) if verdict is None else {"verdict": verdict, **meta}
    with connect_closing() as conn:
        assert kb.complete_task(conn, task_id, summary=f"verdict {verdict}", metadata=metadata), task_id


def _task_count() -> int:
    from hermes_cli.kanban_db_connect import connect_closing

    with connect_closing() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])


def _trail_rows(decision: str, tenant: Optional[str] = None) -> list[dict[str, Any]]:
    from factonio.trail import query_decisions

    return [r for r in query_decisions(tenant=tenant, limit=500) if r["decision"] == decision]


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout.strip()


def _git_env() -> contextlib.AbstractContextManager:
    return mock.patch.dict(os.environ, {
        "GIT_AUTHOR_NAME": "Factonio Test", "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Factonio Test", "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_CONFIG_NOSYSTEM": "1",
    })


class TestCompletionContract(unittest.TestCase):
    def test_ask_forces_local_only_even_with_github_checks(self):
        from factonio.orchestrator import _completion_contract_for_repo

        base = {"repo": "acme/svc", "slug": "acme-svc"}
        with isolated_hermes_home():
            for extra in ({"has_required_checks": True}, {"ci_contract": "github"},
                          {"ci_contract": "auto", "has_required_checks": True}):
                with self.subTest(**extra):
                    self.assertEqual(_completion_contract_for_repo({**base, **extra, "publish_policy": "ask"}), "local-only")
                    self.assertEqual(_completion_contract_for_repo({**base, **extra, "publish_policy": "auto"}), "acme/svc")
            self.assertEqual(_completion_contract_for_repo({**base, "publish_policy": "auto"}), "local-only")
            with self.assertRaisesRegex(ValueError, "publish_policy"):
                _completion_contract_for_repo({**base, "ci_contract": "github", "publish_policy": "sometimes"})

    def test_legacy_record_inherits_global_default(self):
        from factonio.config import save_settings
        from factonio.orchestrator import _completion_contract_for_repo

        legacy = {"repo": "acme/svc", "slug": "acme-svc", "ci_contract": "github"}
        with isolated_hermes_home():
            self.assertEqual(_completion_contract_for_repo(legacy), "acme/svc")
            save_settings({"default_publish_policy": "ask"})
            self.assertEqual(_completion_contract_for_repo(legacy), "local-only")


class TestLocalOnlyImplementation(unittest.TestCase):
    SECTION = "## Local-Only Delivery (publish_policy: ask)"

    def _assert_local_only(self, body: str, base: str) -> None:
        self.assertIn("- Publish policy: `ask`", body)
        self.assertIn(self.SECTION, body)
        self.assertIn("Do NOT run `git push` and do NOT run `gh pr create`", body)
        self.assertIn("<= 40 lines", body)
        for key in ("`branch`", f"`base_branch` (`{base}`)", "`head_sha`", "`pr_title`", "`pr_body`"):
            self.assertIn(key, body)
        self.assertIn("factonio_publish_pr", body)
        self.assertNotIn("/holdout", body)

    def test_start_workstream_direct_paths_under_ask(self):
        from factonio.orchestrator import start_workstream

        with isolated_hermes_home() as home:
            _register(home, publish_policy="ask")
            _set_repo_field("acme-svc", ci_contract="github", has_required_checks=True)
            for kwargs in ({"kind": "bug", "skip_define": True}, {"kind": "quick"}):
                with self.subTest(**kwargs):
                    res = start_workstream("acme/svc", "Fix the widget", title=f"w {kwargs['kind']}", **kwargs)
                    self.assertEqual((res["phase"], res["publish_policy"]), ("implement", "ask"))
                    self.assertEqual(res["completion_contract"], "local-only")
                    task = _get_task(res["task_id"])
                    self._assert_local_only(task.body, "main")
                    self.assertNotIn("push the branch, open a PR", task.body)
                    row = next(r for r in _trail_rows("start_workstream", res["tenant"]))
                    self.assertEqual(row["evidence"]["publish_policy"], "ask")
                    self.assertEqual(row["evidence"]["completion_contract"], "local-only")

    def test_start_workstream_auto_unchanged(self):
        from factonio.orchestrator import start_workstream

        with isolated_hermes_home() as home:
            _register(home)
            _set_repo_field("acme-svc", ci_contract="github")
            res = start_workstream("acme/svc", "Fix the widget", kind="quick")
            self.assertEqual((res["publish_policy"], res["completion_contract"]), ("auto", "acme/svc"))
            body = _get_task(res["task_id"]).body
            self.assertIn("- Publish policy: `auto`", body)
            self.assertIn("push the branch, open a PR against `main`", body)
            self.assertNotIn(self.SECTION, body)
            define = start_workstream("acme/svc", "Add a feature", kind="feature", title="feat")
            self.assertEqual((define["phase"], define["publish_policy"]), ("define", "auto"))
            self.assertNotIn(self.SECTION, _get_task(define["task_id"]).body)

    def test_invalid_policy_fails_closed_before_any_card(self):
        from factonio.orchestrator import spawn_implementation_card, start_workstream

        with isolated_hermes_home() as home:
            _register(home)
            _set_repo_field("acme-svc", publish_policy="sometimes")
            with self.assertRaisesRegex(ValueError, "publish_policy"):
                start_workstream("acme/svc", "Fix", kind="quick")
            with self.assertRaisesRegex(ValueError, "publish_policy"):
                spawn_implementation_card("acme/svc", "t-x", "Unit", "Do it")
            self.assertEqual(_task_count(), 0)

    def test_spawn_implementation_card_ask_auto_and_stacked(self):
        from factonio.orchestrator import spawn_implementation_card

        with isolated_hermes_home() as home:
            _register(home, publish_policy="ask")
            _set_repo_field("acme-svc", has_required_checks=True)
            res = spawn_implementation_card("acme/svc", "t-ask", "Unit 1", "Do the thing")
            self.assertEqual((res["publish_policy"], res["completion_contract"]), ("ask", "local-only"))
            self._assert_local_only(_get_task(res["task_id"]).body, "main")
            row = _trail_rows("spawn_implement", "t-ask")[0]
            self.assertEqual(row["evidence"]["publish_policy"], "ask")

            stacked = spawn_implementation_card("acme/svc", "t-ask", "Unit 2", "Next", base_branch="feat/base")
            body = _get_task(stacked["task_id"]).body
            self._assert_local_only(body, "feat/base")
            self.assertIn("## Stacked PR", body)
            self.assertNotIn("gh pr create --base", body)

            _set_repo_field("acme-svc", publish_policy="auto")
            auto = spawn_implementation_card("acme/svc", "t-auto", "Unit 1", "Do it", base_branch="feat/base")
            self.assertEqual((auto["publish_policy"], auto["completion_contract"]), ("auto", "acme/svc"))
            body = _get_task(auto["task_id"]).body
            self.assertIn("- Publish policy: `auto`", body)
            self.assertIn("gh pr create --base feat/base", body)
            self.assertNotIn(self.SECTION, body)


class TestLocalVerifyTargetValidation(unittest.TestCase):
    def test_fail_closed_inputs_create_nothing(self):
        from factonio.orchestrator import spawn_verification_pair

        bad_cases = [
            ({}, "pr_number .*or branch"),
            ({"pr_number": 3, "branch": "feat/x", "head_sha": SHA}, "not both"),
            ({"branch": "feat/x"}, "head_sha"),
            ({"branch": "feat/x", "head_sha": "abc1234"}, "head_sha"),
            ({"branch": "feat/x", "head_sha": "g" * 40}, "head_sha"),
            ({"branch": "feat/x", "head_sha": SHA + "0"}, "head_sha"),
            ({"branch": "main", "head_sha": SHA}, "must differ"),
            ({"branch": "dev", "base_branch": "dev", "head_sha": SHA}, "must differ"),
            ({"branch": "", "head_sha": SHA}, "branch"),
            ({"branch": "  ", "head_sha": SHA}, "branch"),
            ({"branch": "@", "head_sha": SHA}, "branch"),
            ({"branch": "HEAD", "head_sha": SHA}, "branch"),
            ({"branch": "main~1", "head_sha": SHA}, "branch"),
            ({"branch": "x@{-1}", "head_sha": SHA}, "branch"),
            ({"branch": "a..b", "head_sha": SHA}, "branch"),
            ({"branch": "-x", "head_sha": SHA}, "branch"),
            ({"branch": "feat x", "head_sha": SHA}, "branch"),
            ({"branch": "feat\x00x", "head_sha": SHA}, "branch"),
            ({"branch": "feat\x07x", "head_sha": SHA}, "branch"),
            ({"branch": "feat\ud800x", "head_sha": SHA}, "branch"),
            ({"branch": "feat`x", "head_sha": SHA}, "backtick"),
            ({"branch": "feat.lock", "head_sha": SHA}, "branch"),
            ({"branch": 7, "head_sha": SHA}, "branch"),
            ({"branch": "feat/x", "base_branch": "@", "head_sha": SHA}, "base_branch"),
            ({"branch": "feat/x", "base_branch": "", "head_sha": SHA}, "base_branch"),
            ({"branch": "feat/x", "base_branch": "ma\nin", "head_sha": SHA}, "base_branch"),
            ({"pr_number": 3, "base_branch": "main"}, "base_branch"),
            ({"pr_number": 0}, "pr_number"),
            ({"pr_number": True}, "pr_number"),
            ({"pr_number": "x"}, "pr_number"),
        ]
        with isolated_hermes_home() as home:
            _register(home)
            for kwargs, pattern in bad_cases:
                with self.subTest(kwargs=kwargs):
                    with self.assertRaisesRegex(ValueError, pattern):
                        spawn_verification_pair("acme/svc", "t-bad", **kwargs)
                    with self.assertRaisesRegex(ValueError, pattern):
                        spawn_verification_pair("acme/svc", "t-bad", rigor="high", panel_models=TWO_VENDORS, **kwargs)
            self.assertEqual(_task_count(), 0)
            self.assertEqual(_trail_rows("spawn_verify"), [])

    def test_tool_passes_raw_inputs_and_schema(self):
        with isolated_hermes_home() as home:
            _register(home)
            tool = _tools()["factonio_spawn_verify"]
            params = tool["schema"]["parameters"]
            self.assertEqual(params["required"], ["repo", "tenant"])
            for key in ("pr_number", "branch", "base_branch", "head_sha"):
                self.assertIn(key, params["properties"])
            handler = tool["handler"]
            for args in (
                {}, {"pr_number": 2, "branch": "feat/x", "head_sha": SHA}, {"branch": "feat/x"},
                {"branch": "feat/x", "head_sha": SHA[:12]}, {"branch": "main", "head_sha": SHA},
                {"branch": None, "head_sha": SHA}, {"pr_number": True}, {"pr_number": 2.5},
                {"branch": "feat/x", "head_sha": SHA, "base_branch": "HEAD"},
            ):
                with self.subTest(args=args):
                    out = json.loads(handler({"repo": "acme/svc", "tenant": "t-tool", **args}))
                    self.assertFalse(out["ok"], out)
            self.assertEqual(_task_count(), 0)
            ok = json.loads(handler({"repo": "acme/svc", "tenant": "t-tool", "branch": "feat/x", "head_sha": SHA.upper()}))
            self.assertTrue(ok["ok"], ok)
            self.assertEqual((ok["target_mode"], ok["branch"], ok["base_branch"]), ("local", "feat/x", "main"))
            self.assertEqual((ok["head_sha"], ok["pr_number"]), (SHA, None))
            pr = json.loads(handler({"repo": "acme/svc", "tenant": "t-tool", "pr_number": 4}))
            self.assertEqual((pr["target_mode"], pr["pr_number"], pr["branch"]), ("pr", 4, None))


class TestLocalVerifyCards(unittest.TestCase):
    def test_standard_local_pair_content_barrier_and_harness(self):
        from factonio.harness import bootstrap_verify_harness
        from factonio.orchestrator import holdout_dir_path, spawn_verification_pair

        with isolated_hermes_home() as home:
            repo = _register(home, git_repo=True)
            bootstrap_verify_harness("acme/svc")
            res = spawn_verification_pair(
                "acme/svc", "t-loc", branch="feat/x", base_branch="develop", head_sha=SHA256,
                impl_card_id=None, run_hint="python3 -m unittest",
            )
            self.assertEqual((res["target_mode"], res["pr_number"], res["rigor"]), ("local", None, "standard"))
            self.assertEqual((res["branch"], res["base_branch"], res["head_sha"]), ("feat/x", "develop", SHA256))
            self.assertTrue(res["harness_present"])
            diff, bb = _get_task(res["verify_diff_task_id"]), _get_task(res["verify_blackbox_task_id"])
            self.assertEqual(diff.title, "[verify:diff] acme/svc@feat/x")
            self.assertEqual(bb.title, "[verify:blackbox] acme/svc@feat/x")
            stamp = f"- Local branch target: `feat/x` (base=`develop`, head=`{SHA256}`)"
            holdout = str(holdout_dir_path("t-loc"))
            for task in (diff, bb):
                self.assertIn(stamp, task.body)
                self.assertIn(f"- Expected HEAD SHA: `{SHA256}`", task.body)
                self.assertNotIn("acme/svc#", task.body)
                self.assertEqual(task.workspace_kind, "scratch")

            self.assertEqual(diff.skills, ["factonio:verify-diff"])
            self.assertIn(f"git -C {repo} rev-parse refs/heads/feat/x", diff.body)
            self.assertIn(f"git -C {repo} diff <base_ref>...{SHA256}", diff.body)
            self.assertIn("`origin/develop`", diff.body)
            self.assertIn("`refs/heads/develop`", diff.body)
            self.assertIn("skip `gh pr diff`, `factonio_pr_gate`", diff.body)
            self.assertNotIn("gh pr diff feat", diff.body)
            self.assertNotIn(holdout, diff.body)
            self.assertNotIn(".factonio", diff.body)
            self.assertIn("Do not inspect any `holdout/` directory", diff.body)

            self.assertEqual(bb.skills, ["factonio:verify-blackbox"])
            self.assertIn(
                f"git clone --no-hardlinks {repo} app && cd app && git checkout --detach {SHA256}", bb.body
            )
            self.assertIn("`git rev-parse HEAD`", bb.body)
            self.assertIn(holdout, bb.body)
            self.assertIn("## Repo Verification Harness", bb.body)
            self.assertIn(str(repo / ".factonio" / "verify.md"), bb.body)
            self.assertIn(f"fresh checkout of `{SHA256}`", bb.body)
            self.assertIn("NEVER run `git diff`", bb.body)
            self.assertIn("NEVER read implementation source code", bb.body)
            self.assertNotIn("gh pr diff", bb.body)
            self.assertIn("python3 -m unittest", bb.body)

            row = _trail_rows("spawn_verify", "t-loc")[0]
            ev = row["evidence"]
            self.assertEqual((ev["target_mode"], ev["branch"], ev["base_branch"], ev["head_sha"]),
                             ("local", "feat/x", "develop", SHA256))
            self.assertIsNone(ev["pr_number"])
            self.assertIn("feat/x", row["why"])

    def test_pr_target_unchanged(self):
        from factonio.orchestrator import spawn_verification_pair

        with isolated_hermes_home() as home:
            _register(home)
            res = spawn_verification_pair("acme/svc", "t-pr", 5, head_sha=SHA)
            self.assertEqual((res["target_mode"], res["pr_number"], res["branch"], res["base_branch"]), ("pr", 5, None, None))
            diff = _get_task(res["verify_diff_task_id"])
            self.assertEqual(diff.title, "[verify:diff] acme/svc#5")
            self.assertIn("gh pr diff 5 --repo acme/svc", diff.body)
            self.assertNotIn("Local branch target", diff.body)

    def test_high_rigor_local_panel(self):
        from factonio.harness import bootstrap_verify_harness
        from factonio.orchestrator import holdout_dir_path, spawn_verification_pair

        with isolated_hermes_home() as home:
            repo = _register(home, git_repo=True)
            bootstrap_verify_harness("acme/svc")
            with self.assertRaisesRegex(ValueError, "single vendor family"):
                spawn_verification_pair(
                    "acme/svc", "t-hl", branch="feat/x", head_sha=SHA, rigor="high",
                    panel_models=["anthropic/claude-opus-4-6", "anthropic/claude-sonnet-4-6"],
                )
            self.assertEqual(_task_count(), 0)
            res = spawn_verification_pair(
                "acme/svc", "t-hl", branch="feat/x", head_sha=SHA, rigor="high", panel_models=TWO_VENDORS,
            )
            self.assertEqual((res["rigor"], res["target_mode"], res["pr_number"]), ("high", "local", None))
            panel = res["panel"]
            self.assertEqual(panel["vendors"], ["anthropic", "openai"])
            self.assertEqual(_task_count(), 4)
            holdout = str(holdout_dir_path("t-hl"))
            for lane in panel["lanes"]:
                diff, bb = _get_task(lane["verify_diff_task_id"]), _get_task(lane["verify_blackbox_task_id"])
                with self.subTest(vendor=lane["vendor"]):
                    self.assertEqual(diff.title, f"[verify:diff] acme/svc@feat/x [panel:{lane['vendor']}]")
                    self.assertEqual(bb.title, f"[verify:blackbox] acme/svc@feat/x [panel:{lane['vendor']}]")
                    for task in (diff, bb):
                        self.assertIn(f"Verification panel: `{panel['panel_id']}`", task.body)
                        self.assertIn(f"- Local branch target: `feat/x` (base=`main`, head=`{SHA}`)", task.body)
                        self.assertIn("## Verification Panel Lane", task.body)
                    self.assertNotIn(holdout, diff.body)
                    self.assertIn(f"git -C {repo} diff <base_ref>...{SHA}", diff.body)
                    self.assertIn(holdout, bb.body)
                    self.assertIn("## Repo Verification Harness", bb.body)
                    self.assertIn("--no-hardlinks", bb.body)
            ev = _trail_rows("spawn_verify", "t-hl")[0]["evidence"]
            self.assertEqual((ev["panel_id"], ev["target_mode"], ev["branch"], ev["head_sha"]),
                             (panel["panel_id"], "local", "feat/x", SHA))

    def test_blackbox_checkout_command_isolates_unpushed_worktree_commit(self):
        with tempfile.TemporaryDirectory() as tmp, _git_env():
            root = Path(tmp)
            main = root / "main"
            main.mkdir()
            _git(main, "init", "-q", "-b", "main")
            (main / "a.txt").write_text("a\n", encoding="utf-8")
            _git(main, "add", "-A")
            _git(main, "commit", "-q", "-m", "init")
            wt = root / "wt"
            _git(main, "worktree", "add", "-q", "-b", "feat/local-only", str(wt))
            (wt / "b.txt").write_text("b\n", encoding="utf-8")
            _git(wt, "add", "-A")
            _git(wt, "commit", "-q", "-m", "feature")
            sha = _git(wt, "rev-parse", "HEAD")
            self.assertEqual(_git(main, "rev-parse", "refs/heads/feat/local-only"), sha)
            scratch = root / "scratch"
            scratch.mkdir()
            cmd = f"git clone -q --no-hardlinks {main} app && cd app && git checkout -q --detach {sha} && git rev-parse HEAD"
            proc = subprocess.run(["sh", "-c", cmd], cwd=str(scratch), capture_output=True, text=True, check=False)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), sha)
            self.assertTrue((scratch / "app" / "b.txt").is_file())
            self.assertEqual(_git(main, "rev-parse", "HEAD"), _git(main, "rev-parse", "refs/heads/main"))


class TestLocalTargetMetrics(unittest.TestCase):
    def test_extract_local_target(self):
        from factonio.metrics import _extract_local_target

        body = (
            "## Factonio Diff Verification Card (local branch target)\n- Repository: `acme/svc` (`/x`)\n"
            f"- Local branch target: `feat/x#12` (base=`main`, head=`{SHA}`)\n"
        )
        got = _extract_local_target({"body": body}, [])
        self.assertEqual(got, {"repo": "acme/svc", "branch": "feat/x#12", "base_branch": "main", "head_sha": SHA,
                               "source": "card"})
        meta = {"metadata": json.dumps({"branch": "feat/y", "base_branch": "main", "head_sha": SHA.upper(),
                                        "repo": "acme/svc", "pr_title": "t"})}
        got = _extract_local_target({"body": "impl"}, [meta])
        self.assertEqual((got["branch"], got["head_sha"], got["source"]), ("feat/y", SHA, "metadata"))
        self.assertEqual(_extract_local_target({"body": body}, [meta])["source"], "card")
        for bad in (
            {"branch": "feat/y", "head_sha": "abc"},
            {"branch": "feat/y"},
            {"branch": "", "head_sha": SHA},
            {"branch": "feat y", "head_sha": SHA},
            {"branch": "feat/y", "head_sha": SHA, "pr_number": 3},
            {"branch": "feat/y", "head_sha": SHA, "published_pr": "https://github.com/a/b/pull/3"},
        ):
            with self.subTest(bad=bad):
                self.assertIsNone(_extract_local_target({"body": ""}, [{"metadata": json.dumps(bad)}]))
        self.assertIsNone(_extract_local_target({"body": "- Local branch target: `x` (base=`m`, head=`abc`)"}, []))

    def test_panel_branch_filter_consensus_trail_and_cli(self):
        from factonio import trail
        from factonio.metrics import thread_cards
        from factonio.orchestrator import evaluate_verification_panel, spawn_verification_pair, workstream_status

        with isolated_hermes_home() as home:
            _register(home)
            pr_panel = spawn_verification_pair("acme/svc", "t-lp", 9, rigor="high", panel_models=TWO_VENDORS)
            loc = spawn_verification_pair(
                "acme/svc", "t-lp", branch="feat/x#12", head_sha=SHA, rigor="high", panel_models=TWO_VENDORS,
            )
            other = spawn_verification_pair(
                "acme/svc", "t-lp", branch="feat/other", head_sha=SHA256, rigor="high", panel_models=TWO_VENDORS,
            )
            pid = loc["panel"]["panel_id"]
            ev = evaluate_verification_panel(tenant="t-lp", branch="feat/x#12", record_trail=False)
            self.assertTrue(ev["ok"], ev)
            self.assertEqual((ev["panel_id"], ev["consensus"], ev["pr"]), (pid, "pending", None))
            self.assertEqual(ev["local_target"]["branch"], "feat/x#12")
            self.assertEqual(ev["local_target"]["head_sha"], SHA)
            self.assertEqual(ev["other_panel_ids"], [])
            for lane in ev["lanes"]:
                self.assertIsNone(lane["pr"])
                self.assertEqual(lane["local_target"]["repo"], "acme/svc")

            by_pr = evaluate_verification_panel(tenant="t-lp", pr_number=9, record_trail=False)
            self.assertEqual(by_pr["panel_id"], pr_panel["panel"]["panel_id"])
            self.assertIsNone(by_pr["local_target"])
            missing = evaluate_verification_panel(branch="nope", record_trail=False)
            self.assertFalse(missing["ok"])
            self.assertIn("branch=nope", missing["error"])
            with self.assertRaisesRegex(ValueError, "not both"):
                evaluate_verification_panel(pr_number=9, branch="feat/x#12")
            with self.assertRaisesRegex(ValueError, "branch"):
                evaluate_verification_panel(branch="  ")

            for lane in loc["panel"]["lanes"]:
                _complete(lane["verify_diff_task_id"], "pass", branch="feat/x#12", head_sha=SHA)
                _complete(lane["verify_blackbox_task_id"], "pass", scenarios_run=2)
            ev = evaluate_verification_panel(branch="feat/x#12")
            self.assertEqual((ev["consensus"], ev["merge_allowed"], ev["recorded"]), ("pass", True, True))
            rows = _trail_rows("verify_panel_consensus", "t-lp")
            self.assertEqual(len(rows), 1)
            evidence = rows[0]["evidence"]
            self.assertEqual((evidence["panel_id"], evidence["branch"], evidence["base_branch"], evidence["head_sha"]),
                             (pid, "feat/x#12", "main", SHA))
            self.assertIsNone(evidence["pr_number"])
            self.assertEqual(rows[0]["repo_slug"], "acme-svc")
            audit = trail.audit_trail_evidence(tenant="t-lp")
            self.assertTrue(audit["clean"], audit["findings"])
            self.assertFalse(evaluate_verification_panel(branch="feat/x#12")["recorded"])

            for lane in other["panel"]["lanes"]:
                _complete(lane["verify_diff_task_id"], "reject", findings=["bad"])
            rej = evaluate_verification_panel(branch="feat/other")
            self.assertEqual((rej["consensus"], rej["merge_allowed"]), ("reject", False))
            # A rejected local panel is a real open escalation, never weak evidence.
            findings = trail.audit_trail_evidence(tenant="t-lp")["findings"]
            self.assertEqual([(f["kind"], f["result"]) for f in findings], [("unclosed_escalation", "reject")])
            self.assertEqual(_trail_rows("verify_panel_consensus", "t-lp")[0]["evidence"]["branch"], "feat/other")

            tools = _tools()
            props = tools["factonio_evaluate_panel"]["schema"]["parameters"]["properties"]
            self.assertEqual(props["branch"]["type"], "string")
            out = json.loads(tools["factonio_evaluate_panel"]["handler"]({"branch": "feat/x#12", "record": False}))
            self.assertTrue(out["ok"], out)
            self.assertEqual((out["panel_id"], out["consensus"]), (pid, "pass"))
            self.assertFalse(json.loads(tools["factonio_evaluate_panel"]["handler"]({"branch": "zzz"}))["ok"])

            cards = {c["id"]: c for c in thread_cards(home / "kanban.db", tenant="t-lp")["cards"]}
            card = cards[loc["verify_diff_task_id"]]
            self.assertIsNone(card["pr"])
            self.assertEqual((card["local_target"]["branch"], card["local_target"]["source"]), ("feat/x#12", "card"))
            self.assertIsNone(cards[pr_panel["verify_diff_task_id"]]["local_target"])
            self.assertEqual(cards[pr_panel["verify_diff_task_id"]]["pr"]["number"], 9)

            status = workstream_status(tenant="t-lp")
            self.assertEqual(status["settings_publish_policy"], "auto")
            panels = {p["panel_id"]: p for p in status["panels"]}
            self.assertEqual(panels[pid]["local_target"]["branch"], "feat/x#12")

            run = _cli()
            code, text, _ = run("panel", "--branch", "feat/x#12")
            self.assertEqual(code, 0)
            self.assertIn(f"PASS: panel {pid} acme/svc@feat/x#12 (merge allowed)", text)
            self.assertIn(f"Local target: feat/x#12 (base main) at {SHA}", text)
            code, text, _ = run("panel", "--branch", "feat/x#12", "--json")
            self.assertEqual((code, json.loads(text)["panel_id"]), (0, pid))
            code, _, err = run("panel", "--branch", "missing")
            self.assertEqual(code, 1)
            self.assertIn("No verification panel", err)
            code, _, err = run("panel", "--pr", "9", "--branch", "feat/x#12")
            self.assertEqual(code, 1)
            self.assertIn("not both", err)

            code, text, _ = run("status", "--tenant", "t-lp")
            self.assertEqual(code, 0)
            self.assertIn("[acme/svc@feat/x#12]", text)
            self.assertIn("[acme/svc#9]", text)
            self.assertIn(f"Panel {pid}: pass (4/4 lanes done) [acme/svc@feat/x#12]", text)
            code, text, _ = run("--json", "status", "--tenant", "t-lp")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(text)["settings_publish_policy"], "auto")

    def test_implementation_handoff_local_target_and_settings_policy(self):
        from factonio.config import save_settings
        from factonio.metrics import thread_cards
        from factonio.orchestrator import spawn_implementation_card, workstream_status

        with isolated_hermes_home() as home:
            _register(home, publish_policy="ask")
            impl = spawn_implementation_card("acme/svc", "t-hand", "Unit", "Do it")
            _complete(impl["task_id"], None, branch="factonio/t_1", base_branch="main", head_sha=SHA,
                      pr_title="Add x", pr_body="## Why\nx", repo="acme/svc")
            card = next(c for c in thread_cards(home / "kanban.db", tenant="t-hand")["cards"] if c["id"] == impl["task_id"])
            self.assertEqual(card["local_target"], {"repo": "acme/svc", "branch": "factonio/t_1", "base_branch": "main",
                                                    "head_sha": SHA, "source": "metadata"})
            self.assertIsNone(card["pr"])
            run = _cli()
            code, text, _ = run("status", "--tenant", "t-hand")
            self.assertIn("[acme/svc@factonio/t_1]", text)

            save_settings({"default_publish_policy": "ask"})
            self.assertEqual(workstream_status(tenant="t-hand")["settings_publish_policy"], "ask")
            tool = json.loads(_tools()["factonio_status"]["handler"]({"tenant": "t-hand"}))
            self.assertEqual(tool["settings_publish_policy"], "ask")
            save_settings({"default_publish_policy": "bogus"})
            status = workstream_status(tenant="t-hand")
            self.assertIsNone(status["settings_publish_policy"])
            self.assertIn("default_publish_policy", status["settings_publish_policy_error"])


if __name__ == "__main__":
    unittest.main()
