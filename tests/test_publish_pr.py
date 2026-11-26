"""Clean-room tests for publish_policy settings and the fail-closed `publish_pr` publish action."""

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
from typing import Any, Optional
from unittest import mock

from tests._env import PLUGIN_ROOT, isolated_hermes_home

if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))


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


def _commit(repo: Path, name: str, content: str, msg: str) -> str:
    (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


def _proc(stdout: str = "", rc: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["gh"], returncode=rc, stdout=stdout, stderr=stderr)


def _store(home: Path) -> dict:
    return json.loads((home / "plugin-data" / "factonio" / "repos.json").read_text(encoding="utf-8"))


def _register_stub(home: Path, spec: str = "acme/sample", **kw: Any) -> dict:
    from factonio.repos import add_repo

    local = home / "dev" / spec.split("/")[1]
    (local / ".git").mkdir(parents=True, exist_ok=True)
    return add_repo(spec, local_path=str(local), clone=False, inspect_remote=False, **kw)


class PublishFixture:
    """Bare `origin` + registered operator clone `local` with an unpublished `feat/x` branch."""

    def __init__(self, home: Path) -> None:
        self.origin = home / "origin.git"
        _git(home, "init", "-q", "--bare", "-b", "main", str(self.origin))
        seed = home / "seed"
        _git(home, "clone", "-q", str(self.origin), str(seed))
        _git(seed, "symbolic-ref", "HEAD", "refs/heads/main")
        self.base_sha = _commit(seed, "README", "hello\n", "init")
        _git(seed, "push", "-q", "origin", "main")
        self.local = home / "dev" / "sample"
        self.local.parent.mkdir(parents=True, exist_ok=True)
        _git(home, "clone", "-q", str(self.origin), str(self.local))
        _git(self.local, "checkout", "-q", "-b", "feat/x")
        self.sha = _commit(self.local, "x.txt", "x\n", "feat x")
        _git(self.local, "checkout", "-q", "main")

        from factonio.repos import add_repo

        add_repo("acme/sample", local_path=str(self.local), clone=False, inspect_remote=False)

    def origin_ref(self, branch: str) -> Optional[str]:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=str(self.origin), capture_output=True, text=True, check=False,
        )
        return proc.stdout.strip() or None


class FakeGh:
    """Fake `gh` for `pr create` / `pr view`; snapshots the --body-file content during the call."""

    def __init__(self, *, head: Optional[str] = None, base: str = "main", number: int = 42,
                 create_rc: int = 0, view_rc: int = 0, view_stdout: Optional[str] = None) -> None:
        self.head = head
        self.base = base
        self.number = number
        self.create_rc = create_rc
        self.view_rc = view_rc
        self.view_stdout = view_stdout
        self.calls: list[list[str]] = []
        self.body_path: Optional[str] = None
        self.body_text: Optional[str] = None
        self.draft = False

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        if args[:2] == ["pr", "create"]:
            self.body_path = args[args.index("--body-file") + 1]
            self.body_text = Path(self.body_path).read_text(encoding="utf-8")
            self.draft = "--draft" in args
            if self.create_rc:
                return _proc("", rc=self.create_rc, stderr="a pull request already exists")
            return _proc(f"https://github.com/acme/sample/pull/{self.number}\n")
        if args[:2] == ["pr", "view"]:
            if self.view_rc:
                return _proc("", rc=self.view_rc, stderr="not found")
            if self.view_stdout is not None:
                return _proc(self.view_stdout)
            return _proc(json.dumps({
                "number": self.number,
                "url": f"https://github.com/acme/sample/pull/{self.number}",
                "headRefOid": self.head,
                "baseRefName": self.base,
                "state": "OPEN",
                "isDraft": self.draft,
            }))
        raise AssertionError(f"unexpected gh call {args}")


def _publish_rows() -> list[dict]:
    from factonio import trail

    return [r for r in trail.query_decisions(limit=500) if r["decision"] == "publish_pr"]


class TestPublishPolicyConfig(unittest.TestCase):
    def test_defaults_and_manifest_schema(self):
        import hermes_yaml as yaml
        from factonio.config import DEFAULT_SETTINGS, PUBLISH_POLICIES

        self.assertEqual(PUBLISH_POLICIES, ("auto", "ask"))
        self.assertEqual(DEFAULT_SETTINGS["default_publish_policy"], "auto")
        manifest = yaml.safe_load((PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8"))
        schema = manifest["config_schema"]["default_publish_policy"]
        self.assertEqual((schema["type"], schema["choices"], schema["default"]), ("str", ["auto", "ask"], "auto"))
        self.assertIn("factonio_publish_pr", manifest["provides_tools"])

    def test_resolution_precedence(self):
        from factonio.config import resolve_publish_policy, save_settings

        with isolated_hermes_home() as home:
            self.assertEqual(resolve_publish_policy(), "auto")
            save_settings({"default_publish_policy": "ask"})
            self.assertEqual(resolve_publish_policy(), "ask")
            self.assertEqual(resolve_publish_policy(repo={"slug": "legacy"}), "ask")
            self.assertEqual(resolve_publish_policy(repo={"publish_policy": "AUTO "}), "auto")
            self.assertEqual(resolve_publish_policy(repo={"publish_policy": "auto"}, override="ask"), "ask")

            _register_stub(home, publish_policy="auto")
            self.assertEqual(resolve_publish_policy(repo_slug="acme-sample"), "auto")
            self.assertEqual(resolve_publish_policy(repo_slug="acme/sample", override="ask"), "ask")
            self.assertEqual(resolve_publish_policy(repo_slug="nope-missing"), "ask")

    def test_invalid_values_raise(self):
        from factonio.config import resolve_publish_policy, save_settings

        with isolated_hermes_home():
            for kwargs in ({"override": "always"}, {"override": ""}, {"repo": {"publish_policy": "never"}},
                           {"repo": {"publish_policy": 1}}):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    resolve_publish_policy(**kwargs)
            save_settings({"default_publish_policy": "sometimes"})
            with self.assertRaises(ValueError):
                resolve_publish_policy()


    def test_explicit_null_or_empty_fails_closed_but_missing_key_inherits(self):
        from factonio.config import list_repos, resolve_publish_policy, save_settings

        with isolated_hermes_home() as home:
            for bad in (None, "", "  "):
                with self.subTest(repo_value=bad), self.assertRaises(ValueError):
                    resolve_publish_policy(repo={"slug": "x", "publish_policy": bad})
            _register_stub(home, publish_policy="ask")
            for bad in (None, ""):
                with self.subTest(repos_json=bad):
                    repos = list_repos()
                    repos["acme-sample"]["publish_policy"] = bad
                    save_settings({"repos": repos})
                    self.assertIn("publish_policy", _store(home)["acme-sample"])
                    with self.assertRaises(ValueError):
                        resolve_publish_policy(repo_slug="acme-sample")
                    self.assertEqual(resolve_publish_policy(repo_slug="acme-sample", override="ask"), "ask")

            for bad in (None, ""):
                with self.subTest(config_yaml=bad):
                    save_settings({"default_publish_policy": bad})
                    with self.assertRaises(ValueError):
                        resolve_publish_policy()
                    with self.assertRaises(ValueError):
                        resolve_publish_policy(repo={"slug": "legacy"})
                    self.assertEqual(resolve_publish_policy(repo={"publish_policy": "ask"}), "ask")
            save_settings({"default_publish_policy": "ask"})
            self.assertEqual(resolve_publish_policy(repo={"slug": "legacy"}), "ask")


class TestRepoPublishPolicy(unittest.TestCase):
    def test_validator_and_editable_fields(self):
        from factonio.repos import _CLEARABLE_REPO_FIELDS, EDITABLE_REPO_FIELDS, _validate_publish_policy

        self.assertIn("publish_policy", EDITABLE_REPO_FIELDS)
        self.assertNotIn("publish_policy", _CLEARABLE_REPO_FIELDS)
        self.assertEqual(_validate_publish_policy(" ASK "), "ask")
        self.assertEqual(_validate_publish_policy("auto"), "auto")
        for bad in (None, "", "inherit", "always", 1, True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                _validate_publish_policy(bad)

    def test_add_defaults_explicit_and_readd_preserves(self):
        from factonio.config import save_settings

        with isolated_hermes_home() as home:
            self.assertEqual(_register_stub(home)["repo"]["publish_policy"], "auto")
            self.assertEqual(_register_stub(home, publish_policy="ask")["repo"]["publish_policy"], "ask")
            save_settings({"default_publish_policy": "auto"})
            self.assertEqual(_register_stub(home)["repo"]["publish_policy"], "ask")
            self.assertEqual(_store(home)["acme-sample"]["publish_policy"], "ask")

            save_settings({"default_publish_policy": "ask"})
            self.assertEqual(_register_stub(home, "acme/other")["repo"]["publish_policy"], "ask")

    def test_add_invalid_publish_policy_has_no_side_effects(self):
        from factonio.repos import add_repo

        with isolated_hermes_home() as home:
            local = home / "dev" / "sample"
            with mock.patch("factonio.repos._run_gh") as gh, self.assertRaises(ValueError):
                add_repo("acme/sample", local_path=str(local), publish_policy="always")
            gh.assert_not_called()
            self.assertFalse(local.exists())
            self.assertFalse((home / "plugin-data" / "factonio" / "repos.json").exists())

    def test_add_fails_closed_on_persisted_null_or_empty(self):
        from factonio.config import list_repos, save_settings

        for bad in (None, ""):
            with self.subTest(config_yaml=bad), isolated_hermes_home() as home:
                save_settings({"default_publish_policy": bad})
                with self.assertRaises(ValueError):
                    _register_stub(home)
                self.assertFalse((home / "plugin-data" / "factonio" / "repos.json").exists())
                self.assertEqual(_register_stub(home, publish_policy="ask")["repo"]["publish_policy"], "ask")

            with self.subTest(repos_json=bad), isolated_hermes_home() as home:
                _register_stub(home, publish_policy="ask")
                repos = list_repos()
                repos["acme-sample"]["publish_policy"] = bad
                save_settings({"repos": repos})
                before = _store(home)
                with self.assertRaises(ValueError):
                    _register_stub(home)
                self.assertEqual(_store(home), before)
                self.assertEqual(_register_stub(home, publish_policy="auto")["repo"]["publish_policy"], "auto")

        with isolated_hermes_home() as home:
            _register_stub(home, publish_policy="ask")
            repos = list_repos()
            repos["acme-sample"].pop("publish_policy")
            save_settings({"repos": repos, "default_publish_policy": "ask"})
            self.assertEqual(_register_stub(home)["repo"]["publish_policy"], "ask")

    def test_update_publish_policy(self):
        from factonio.repos import update_repo

        with isolated_hermes_home() as home:
            _register_stub(home, merge_policy="ask")
            res = update_repo("acme/sample", publish_policy="ASK")
            self.assertEqual(res["updated_fields"], ["publish_policy"])
            stored = _store(home)["acme-sample"]
            self.assertEqual((stored["publish_policy"], stored["merge_policy"]), ("ask", "ask"))
            before = _store(home)
            for bad in (None, "", "inherit", "always"):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    update_repo("acme-sample", publish_policy=bad)
            self.assertEqual(_store(home), before)


class TestPublishPr(unittest.TestCase):
    def _publish(self, fx: PublishFixture, gh: FakeGh, **kw: Any) -> tuple[dict, list[list[str]]]:
        from factonio import repos

        args = {"expected_head_sha": fx.sha, "title": "Add x", "body": "## Why\nbecause\n",
                "tenant": "t-pub", "card_id": "t_card"}
        args.update(kw)
        repo = args.pop("repo", "acme/sample")
        branch = args.pop("branch", "feat/x")
        git_calls: list[list[str]] = []
        real_git = repos._run_git

        def spy(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
            git_calls.append(list(cmd))
            return real_git(cmd, **kwargs)

        with mock.patch("factonio.repos._run_gh", gh), mock.patch("factonio.repos._run_git", spy):
            res = repos.publish_pr(repo, branch, **args)
        return res, git_calls

    def test_publishes_exact_sha_with_body_file_and_live_readback(self):
        with isolated_hermes_home(), _git_env():
            fx = PublishFixture(Path(os.environ["HERMES_HOME"]))
            gh = FakeGh(head=fx.sha)
            res, git_calls = self._publish(fx, gh, title="Add x\nsecond line", expected_head_sha=fx.sha.upper())

            self.assertTrue(res["ok"], res)
            self.assertEqual(
                (res["published"], res["pushed"], res["created"], res["refused"]), (True, True, True, False)
            )
            self.assertEqual(
                (res["repo"], res["pr_number"], res["url"], res["state"], res["draft"]),
                ("acme/sample", 42, "https://github.com/acme/sample/pull/42", "OPEN", False),
            )
            self.assertEqual((res["head_sha"], res["expected_head_sha"]), (fx.sha, fx.sha))
            self.assertEqual((res["branch"], res["base_branch"]), ("feat/x", "main"))
            self.assertEqual(fx.origin_ref("feat/x"), fx.sha)
            self.assertIn(["push", "origin", f"{fx.sha}:refs/heads/feat/x"], git_calls)

            create = next(c for c in gh.calls if c[:2] == ["pr", "create"])
            self.assertEqual(
                create[:12],
                ["pr", "create", "--repo", "acme/sample", "--base", "main", "--head", "feat/x",
                 "--title", "Add x second line", "--body-file", gh.body_path],
            )
            self.assertNotIn("--draft", create)
            self.assertEqual(gh.body_text, "## Why\nbecause\n")
            self.assertFalse(Path(gh.body_path).exists())
            view = next(c for c in gh.calls if c[:2] == ["pr", "view"])
            self.assertEqual(view[-1], "number,url,headRefOid,baseRefName,state,isDraft")

            rows = _publish_rows()
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(
                (row["phase"], row["result"], row["tenant"], row["card_id"], row["repo_slug"]),
                ("publish", "published", "t-pub", "t_card", "acme-sample"),
            )
            ev = row["evidence"]
            self.assertEqual(
                {k: ev[k] for k in ("repo", "branch", "base_branch", "head_sha", "expected_head_sha", "pr_number",
                                    "url", "draft")},
                {"repo": "acme/sample", "branch": "feat/x", "base_branch": "main", "head_sha": fx.sha,
                 "expected_head_sha": fx.sha, "pr_number": 42, "url": "https://github.com/acme/sample/pull/42",
                 "draft": False},
            )

    def test_draft_body_file_and_explicit_base(self):
        with isolated_hermes_home() as home, _git_env():
            fx = PublishFixture(home)
            _git(fx.local, "branch", "release", fx.base_sha)
            body = home / "briefing.md"
            body.write_text("Ünïcode body `$(rm -rf /)`\n", encoding="utf-8")
            gh = FakeGh(head=fx.sha, base="release")
            res, _ = self._publish(fx, gh, body=None, body_file=str(body), base_branch="release", draft=True)
            self.assertTrue(res["published"], res)
            self.assertEqual((res["draft"], res["base_branch"]), (True, "release"))
            create = next(c for c in gh.calls if c[:2] == ["pr", "create"])
            self.assertIn("--draft", create)
            self.assertEqual(create[create.index("--base") + 1], "release")
            self.assertEqual(gh.body_text, "Ünïcode body `$(rm -rf /)`\n")
            self.assertFalse(Path(gh.body_path).exists())
            self.assertTrue(body.exists())

    def test_refusals_never_push_or_call_gh(self):
        with isolated_hermes_home() as home, _git_env():
            fx = PublishFixture(home)
            _commit(fx.local, "y.txt", "y\n", "main moved")
            nocheckout = home / "dev" / "nocheckout"
            nocheckout.mkdir(parents=True)
            from factonio.config import list_repos, save_settings

            repos = list_repos()
            repos["acme-gone"] = {"slug": "acme-gone", "repo": "acme/gone", "path": str(nocheckout),
                                  "default_branch": "main"}
            save_settings({"repos": repos})
            body = home / "b.md"
            body.write_text("x", encoding="utf-8")
            evil = fx.local / "evil.git"
            _git(fx.local, "init", "-q", "--bare", str(evil))
            cases = {
                "short sha": ({"expected_head_sha": fx.sha[:12]}, "expected_head_sha"),
                "non-hex sha": ({"expected_head_sha": "g" * 40}, "expected_head_sha"),
                "ref not sha": ({"expected_head_sha": "feat/x"}, "expected_head_sha"),
                "empty title": ({"title": " \n "}, "title"),
                "body and body_file": ({"body_file": str(body)}, "body"),
                "missing body_file": ({"body": None, "body_file": str(home / "missing.md")}, "body_file"),
                "bad branch": ({"branch": "-x"}, "branch must not start"),
                "bad remote": ({"remote": "a/b"}, "remote"),
                "scp remote": ({"remote": "attacker@evil.com:exfil.git"}, "plain remote name"),
                "backslash remote": ({"remote": "a\\b"}, "plain remote name"),
                "empty remote": ({"remote": ""}, "remote"),
                "unconfigured local bare remote": ({"remote": "evil.git"}, "not a configured git remote"),
                "body_file NUL": ({"body": None, "body_file": "brief\x00.md"}, "body_file"),
                "empty body_file": ({"body": None, "body_file": "  "}, "body_file"),
                "non-string body_file": ({"body": None, "body_file": 7}, "body_file"),
                "bad base": ({"base_branch": "a..b"}, "base_branch must not contain"),
                "branch is base": ({"branch": "main", "expected_head_sha": fx.base_sha}, "target base"),
                "unregistered": ({"repo": "nope/missing"}, "not registered"),
                "missing checkout": ({"repo": "acme/gone"}, "checkout"),
                "missing branch": ({"branch": "feat/none"}, "does not exist"),
                "head drift": ({"expected_head_sha": fx.base_sha}, "HEAD drift"),
            }
            for name, (kw, needle) in cases.items():
                with self.subTest(case=name):
                    gh = FakeGh(head=fx.sha)
                    res, git_calls = self._publish(fx, gh, **kw)
                    self.assertFalse(res["ok"], res)
                    self.assertTrue(res["refused"], res)
                    self.assertEqual((res["published"], res["pushed"], res["created"]), (False, False, False))
                    self.assertIn(needle, res["error"])
                    self.assertEqual(gh.calls, [])
                    self.assertFalse(any(c[:1] == ["push"] for c in git_calls), git_calls)
            self.assertIsNone(fx.origin_ref("feat/x"))
            self.assertEqual(_git(evil, "for-each-ref"), "")
            rows = _publish_rows()
            self.assertEqual(len(rows), len(cases))
            self.assertEqual({r["result"] for r in rows}, {"refused"})
            self.assertEqual({r["phase"] for r in rows}, {"publish"})

    def test_drift_after_verification_is_refused(self):
        with isolated_hermes_home() as home, _git_env():
            fx = PublishFixture(home)
            _git(fx.local, "checkout", "-q", "feat/x")
            moved = _commit(fx.local, "z.txt", "z\n", "unverified")
            gh = FakeGh(head=moved)
            res, _ = self._publish(fx, gh)
            self.assertTrue(res["refused"])
            self.assertEqual(res["head_sha"], moved)
            self.assertIn(fx.sha, res["error"])
            self.assertIsNone(fx.origin_ref("feat/x"))
            self.assertEqual(gh.calls, [])

    def test_push_failure_is_error_without_gh(self):
        with isolated_hermes_home() as home, _git_env():
            fx = PublishFixture(home)
            _git(fx.local, "remote", "set-url", "origin", str(home / "missing-origin.git"))
            gh = FakeGh(head=fx.sha)
            res, git_calls = self._publish(fx, gh)
            self.assertIn(["push", "origin", f"{fx.sha}:refs/heads/feat/x"], git_calls)
            self.assertFalse(res["ok"])
            self.assertFalse(res["refused"])
            self.assertEqual((res["pushed"], res["created"], res["published"]), (False, False, False))
            self.assertIn("git push failed", res["error"])
            self.assertEqual(gh.calls, [])
            self.assertEqual(_publish_rows()[0]["result"], "error")

    def test_create_failure_cleans_body_file(self):
        with isolated_hermes_home() as home, _git_env():
            fx = PublishFixture(home)
            gh = FakeGh(head=fx.sha, create_rc=1)
            res, _ = self._publish(fx, gh)
            self.assertEqual((res["ok"], res["pushed"], res["created"], res["published"]), (False, True, False, False))
            self.assertIn("gh pr create failed", res["error"])
            self.assertFalse(Path(gh.body_path).exists())
            self.assertEqual(fx.origin_ref("feat/x"), fx.sha)
            self.assertFalse(any(c[:2] == ["pr", "view"] for c in gh.calls))
            self.assertEqual(_publish_rows()[0]["result"], "error")

    def test_readback_failure_or_mismatch_is_not_published(self):
        with isolated_hermes_home() as home, _git_env():
            fx = PublishFixture(home)
            for name, gh in {
                "view fails": FakeGh(head=fx.sha, view_rc=1),
                "invalid json": FakeGh(head=fx.sha, view_stdout="not json"),
                "head mismatch": FakeGh(head=fx.base_sha),
                "base mismatch": FakeGh(head=fx.sha, base="develop"),
            }.items():
                with self.subTest(case=name):
                    res, _ = self._publish(fx, gh)
                    self.assertEqual((res["ok"], res["published"], res["created"]), (False, False, True))
                    self.assertTrue(res["error"])
            self.assertEqual({r["result"] for r in _publish_rows()}, {"error"})

    def test_publish_rows_audit_cleanly_and_close_escalations(self):
        from factonio import trail

        with isolated_hermes_home() as home, _git_env():
            fx = PublishFixture(home)
            trail.log_decision(
                "verify_local", tenant="t-pub", card_id="t_card", phase="verify", why="awaiting publish approval",
                evidence={"head_sha": fx.sha}, result="needs_input",
            )
            self._publish(fx, FakeGh(head=fx.sha), expected_head_sha="0" * 40)
            self.assertFalse(trail.audit_trail_evidence(tenant="t-pub")["clean"])
            res, _ = self._publish(fx, FakeGh(head=fx.sha))
            self.assertTrue(res["published"])
            audit = trail.audit_trail_evidence(tenant="t-pub")
            self.assertTrue(audit["clean"], audit["findings"])

            trail.log_decision("publish_pr", tenant="t-weak", phase="publish", why="no artifact",
                               evidence={"note": "x"}, result="published")
            kinds = {f["kind"] for f in trail.audit_trail_evidence(tenant="t-weak")["findings"]}
            self.assertEqual(kinds, {"weak_evidence"})


class _Ctx:
    def __init__(self) -> None:
        self.cli: dict[str, Any] = {}
        self.tools: dict[str, dict[str, Any]] = {}

    def register_cli_command(self, name: str, *, help: str, setup_fn: Any, handler_fn: Any) -> None:
        self.cli[name] = (setup_fn, handler_fn)

    def register_tool(self, *, name: str, toolset: str, schema: dict, handler: Any) -> None:
        self.tools[name] = {"schema": schema, "handler": handler}


def _run_cli(*argv: str) -> tuple[int, str, str]:
    from factonio.commands import register_cli

    ctx = _Ctx()
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
    return code, out.getvalue(), err.getvalue()


class TestPublishToolAndCli(unittest.TestCase):
    def test_tools_registered_with_manifest_parity(self):
        import hermes_yaml as yaml
        from factonio.agent_tools import register_tools

        ctx = _Ctx()
        register_tools(ctx)
        manifest = yaml.safe_load((PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8"))
        self.assertEqual(sorted(manifest["provides_tools"]), sorted(ctx.tools))
        schema = ctx.tools["factonio_publish_pr"]["schema"]["parameters"]
        self.assertEqual(schema["required"], ["repo", "branch", "expected_head_sha", "title"])
        self.assertTrue({"body", "body_file", "base_branch", "draft", "remote", "tenant", "card_id"}
                        <= set(schema["properties"]))
        for tool in ("factonio_add_repo", "factonio_update_repo"):
            props = ctx.tools[tool]["schema"]["parameters"]["properties"]
            self.assertEqual(props["publish_policy"]["enum"], ["auto", "ask"])

        handler = ctx.tools["factonio_publish_pr"]["handler"]
        with mock.patch("factonio.agent_tools.publish_pr", return_value={"ok": True, "published": True}) as pub, \
                mock.patch.dict(os.environ, {"HERMES_KANBAN_TASK": "t_worker", "HERMES_TENANT": "ten"}):
            res = json.loads(handler({"repo": "acme/sample", "branch": "feat/x", "expected_head_sha": "a" * 40,
                                      "title": "T", "body": "B", "draft": True}))
        self.assertTrue(res["published"])
        self.assertEqual(pub.call_args.args, ("acme/sample", "feat/x"))
        kw = pub.call_args.kwargs
        self.assertEqual(
            (kw["expected_head_sha"], kw["title"], kw["body"], kw["draft"], kw["remote"], kw["tenant"],
             kw["card_id"], kw["actor"]),
            ("a" * 40, "T", "B", True, "origin", "ten", "t_worker", "worker"),
        )

    def test_publish_tool_passes_raw_input_so_publish_pr_refuses(self):
        from factonio.agent_tools import register_tools

        ctx = _Ctx()
        register_tools(ctx)
        handler = ctx.tools["factonio_publish_pr"]["handler"]
        good = {"repo": "acme/sample", "branch": "feat/x", "expected_head_sha": "a" * 40, "title": "T"}
        cases = {
            "title None": ({**good, "title": None}, "title"),
            "empty base_branch": ({**good, "base_branch": ""}, "base_branch"),
            "empty remote": ({**good, "remote": ""}, "remote"),
            "missing repo": ({k: v for k, v in good.items() if k != "repo"}, "repo"),
            "missing branch": ({k: v for k, v in good.items() if k != "branch"}, "branch"),
            "missing sha": ({k: v for k, v in good.items() if k != "expected_head_sha"}, "expected_head_sha"),
            "missing title": ({k: v for k, v in good.items() if k != "title"}, "title"),
        }
        with isolated_hermes_home(), mock.patch("factonio.repos._run_gh") as gh, \
                mock.patch("factonio.repos._run_git") as git:
            for name, (args, needle) in cases.items():
                with self.subTest(case=name):
                    res = json.loads(handler(args))
                    self.assertEqual((res["ok"], res["refused"], res["published"], res["pushed"], res["created"]),
                                     (False, True, False, False, False), res)
                    self.assertIn(needle, res["error"])
            gh.assert_not_called()
            git.assert_not_called()
            rows = _publish_rows()
            self.assertEqual(len(rows), len(cases))
            self.assertEqual({r["result"] for r in rows}, {"refused"})

        with mock.patch("factonio.agent_tools.publish_pr", return_value={"ok": False}) as pub:
            handler(dict(good))
        kw = pub.call_args.kwargs
        self.assertEqual((kw["base_branch"], kw["remote"]), (None, "origin"))

    def test_add_and_update_tools_persist_publish_policy(self):
        from factonio.agent_tools import register_tools

        with isolated_hermes_home() as home:
            ctx = _Ctx()
            register_tools(ctx)
            local = home / "dev" / "sample"
            (local / ".git").mkdir(parents=True)
            with mock.patch("factonio.repos.inspect_github_repo", return_value={
                "repo": "acme/sample", "url": "https://github.com/acme/sample", "default_branch": "main",
                "required_checks": [], "has_required_checks": False,
            }):
                res = json.loads(ctx.tools["factonio_add_repo"]["handler"](
                    {"repo": "acme/sample", "local_path": str(local), "publish_policy": "ask", "clone": False}
                ))
            self.assertTrue(res["ok"], res)
            self.assertEqual(_store(home)["acme-sample"]["publish_policy"], "ask")
            res = json.loads(ctx.tools["factonio_update_repo"]["handler"]({"repo": "acme-sample", "publish_policy": "auto"}))
            self.assertEqual(res["updated_fields"], ["publish_policy"])
            res = json.loads(ctx.tools["factonio_update_repo"]["handler"]({"repo": "acme-sample", "publish_policy": None}))
            self.assertFalse(res["ok"])
            self.assertEqual(_store(home)["acme-sample"]["publish_policy"], "auto")

    def test_cli_add_update_list_show_publish_policy(self):
        with isolated_hermes_home() as home:
            local = home / "dev" / "sample"
            (local / ".git").mkdir(parents=True)
            with mock.patch("factonio.repos.inspect_github_repo", return_value={
                "repo": "acme/sample", "url": "https://github.com/acme/sample", "default_branch": "main",
                "required_checks": [], "has_required_checks": False,
            }):
                code, out, _ = _run_cli("add", "acme/sample", "--path", str(local), "--no-clone",
                                        "--publish-policy", "ask")
            self.assertEqual(code, 0, out)
            self.assertIn("Publish policy: ask", out)
            code, out, _ = _run_cli("list")
            self.assertIn("publish=ask", out)
            code, out, _ = _run_cli("update", "acme/sample", "--publish-policy", "auto")
            self.assertEqual(code, 0)
            self.assertIn("Publish policy: auto", out)
            code, out, _ = _run_cli("edit", "acme-sample", "--publish-policy", "ask", "--json")
            self.assertEqual(json.loads(out)["repo"]["publish_policy"], "ask")
            code, _, _ = _run_cli("update", "acme/sample", "--publish-policy", "always")
            self.assertNotEqual(code, 0)

            from factonio.config import list_repos, save_settings

            repos = list_repos()
            repos["acme-sample"].pop("publish_policy")
            save_settings({"repos": repos, "default_publish_policy": "ask"})
            code, out, _ = _run_cli("list")
            self.assertIn("publish=ask", out)

    def test_cli_add_and_list_fail_closed_on_null_or_empty_policy(self):
        from factonio.config import list_repos, save_settings

        stub_remote = {"repo": "acme/sample", "url": "https://github.com/acme/sample", "default_branch": "main",
                       "required_checks": [], "has_required_checks": False}
        for bad in (None, ""):
            with self.subTest(config_yaml=bad), isolated_hermes_home() as home, \
                    mock.patch("factonio.repos.inspect_github_repo", return_value=stub_remote):
                local = home / "dev" / "sample"
                (local / ".git").mkdir(parents=True)
                save_settings({"default_publish_policy": bad})
                code, out, _ = _run_cli("--json", "add", "acme/sample", "--path", str(local), "--no-clone")
                self.assertEqual(code, 1, out)
                self.assertIn("default_publish_policy", json.loads(out)["error"])
                self.assertFalse((home / "plugin-data" / "factonio" / "repos.json").exists())
                code, out, err = _run_cli("list")
                self.assertEqual(code, 1)
                self.assertIn("default_publish_policy", err)

            with self.subTest(repos_json=bad), isolated_hermes_home() as home, \
                    mock.patch("factonio.repos.inspect_github_repo", return_value=stub_remote):
                local = home / "dev" / "sample"
                (local / ".git").mkdir(parents=True)
                self.assertEqual(_run_cli("add", "acme/sample", "--path", str(local), "--no-clone")[0], 0)
                repos = list_repos()
                repos["acme-sample"]["publish_policy"] = bad
                save_settings({"repos": repos})
                code, out, err = _run_cli("list")
                self.assertEqual(code, 1)
                self.assertNotIn("publish=auto", out)
                self.assertIn("publish=invalid", out)
                self.assertIn("repos.acme-sample.publish_policy", err)
                code, out, _ = _run_cli("--json", "list")
                self.assertEqual(code, 1)
                self.assertFalse(json.loads(out)["ok"])
                code, out, err = _run_cli("add", "acme/sample", "--path", str(local), "--no-clone")
                self.assertEqual(code, 1, out)
                self.assertIn("publish_policy", err)
                self.assertEqual(_store(home)["acme-sample"]["publish_policy"], bad)

    def test_cli_publish_pr_exit_codes_and_json_placement(self):
        code, out, _ = _run_cli("publish-pr", "--help")
        self.assertEqual(code, 0)
        self.assertIn("--expected-head-sha", out)
        code, _, err = _run_cli("publish-pr", "acme/sample", "feat/x", "--expected-head-sha", "a" * 40,
                                "--title", "T", "--body", "b", "--body-file", "f")
        self.assertEqual(code, 2)
        self.assertIn("not allowed with", err)

        with isolated_hermes_home() as home, _git_env():
            fx = PublishFixture(home)
            with mock.patch("factonio.repos._run_gh", FakeGh(head=fx.sha)):
                code, out, err = _run_cli("publish-pr", "acme/sample", "feat/x", "--expected-head-sha", fx.base_sha,
                                          "--title", "T")
                self.assertEqual(code, 1)
                self.assertIn("Refused: HEAD drift", err)
                code, out, _ = _run_cli("--json", "publish-pr", "acme/sample", "feat/x", "--expected-head-sha",
                                        fx.base_sha, "--title", "T")
                self.assertEqual(code, 1)
                self.assertTrue(json.loads(out)["refused"])
                self.assertIsNone(fx.origin_ref("feat/x"))

                code, out, _ = _run_cli("publish-pr", "acme/sample", "feat/x", "--expected-head-sha", fx.sha,
                                        "--title", "T", "--draft", "--json", "--card-id", "t_c", "--tenant", "ten")
            self.assertEqual(code, 0, out)
            data = json.loads(out)
            self.assertEqual((data["published"], data["draft"], data["pr_number"]), (True, True, 42))
            self.assertEqual(fx.origin_ref("feat/x"), fx.sha)
            row = [r for r in _publish_rows() if r["result"] == "published"][0]
            self.assertEqual((row["actor"], row["tenant"], row["card_id"]), ("cli", "ten", "t_c"))

            with mock.patch("factonio.repos._run_gh", FakeGh(head=fx.sha, number=43)):
                _git(fx.local, "branch", "feat/y", fx.sha)
                code, out, _ = _run_cli("publish-pr", "acme-sample", "feat/y", "--expected-head-sha", fx.sha,
                                        "--title", "Y")
            self.assertEqual(code, 0, out)
            self.assertIn("Published PR acme/sample#43", out)


if __name__ == "__main__":
    unittest.main()
