"""Clean-room tests for `hermes factonio update|edit` CLI and the `factonio_update_repo` agent tool."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from typing import Any

from tests._env import PLUGIN_ROOT, isolated_hermes_home

if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))


class _FakeCtx:
    def __init__(self) -> None:
        self.cli: dict[str, Any] = {}
        self.tools: dict[str, dict[str, Any]] = {}

    def register_cli_command(self, name: str, *, help: str, setup_fn: Any, handler_fn: Any) -> None:
        self.cli[name] = {"help": help, "setup_fn": setup_fn, "handler_fn": handler_fn}

    def register_tool(self, *, name: str, toolset: str, schema: dict, handler: Any) -> None:
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler}


def _register(home: Path, spec: str = "acme/sample") -> dict:
    from factonio.repos import add_repo

    local = home / "dev" / spec.split("/")[1]
    (local / ".git").mkdir(parents=True, exist_ok=True)
    return add_repo(spec, local_path=str(local), clone=False, inspect_remote=False)


def _store(home: Path) -> dict:
    return json.loads((home / "plugin-data" / "factonio" / "repos.json").read_text(encoding="utf-8"))


class TestUpdateCli(unittest.TestCase):
    def _cli(self):
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

    def test_update_and_edit_alias(self):
        with isolated_hermes_home() as home:
            _register(home)
            run = self._cli()

            code, out, _ = run("update", "acme/sample", "--merge-policy", "ask", "--max-fix-rounds", "5")
            self.assertEqual(code, 0)
            self.assertIn("Updated acme/sample", out)
            stored = _store(home)["acme-sample"]
            self.assertEqual((stored["merge_policy"], stored["max_fix_rounds"]), ("ask", 5))

            code, out, _ = run(
                "edit", "acme-sample", "--default-branch", "develop",
                "--design-provider", "hermes", "--design-approval", "auto", "--json",
            )
            self.assertEqual(code, 0)
            res = json.loads(out)
            self.assertTrue(res["ok"])
            self.assertEqual(res["updated_fields"], ["default_branch", "design_approval", "design_provider"])
            self.assertEqual(res["repo"]["merge_policy"], "ask")

            code, out, _ = run("--json", "update", "acme/sample", "--design-provider", "inherit", "--design-approval", "inherit")
            self.assertEqual(code, 0)
            res = json.loads(out)
            self.assertNotIn("design_provider", res["repo"])
            self.assertNotIn("design_approval", _store(home)["acme-sample"])

            code, out, _ = run("list")
            self.assertEqual(code, 0)
            self.assertIn("branch=develop", out)
            self.assertIn("merge=ask", out)
            self.assertIn("fix_rounds=5", out)

    def test_update_errors_exit_nonzero(self):
        with isolated_hermes_home() as home:
            _register(home)
            run = self._cli()
            before = _store(home)

            code, _, err = run("update", "acme/sample")
            self.assertEqual(code, 1)
            self.assertIn("Error:", err)

            code, out, _ = run("update", "acme/sample", "--json")
            self.assertEqual(code, 1)
            self.assertFalse(json.loads(out)["ok"])

            code, out, _ = run("--json", "update", "nope/missing", "--merge-policy", "ask")
            self.assertEqual(code, 1)
            self.assertIn("not registered", json.loads(out)["error"])

            code, _, err = run("update", "acme/sample", "--max-fix-rounds", "0")
            self.assertEqual(code, 1)
            self.assertIn("max_fix_rounds", err)

            code, _, err = run("update", "acme/sample", "--default-branch", "a..b")
            self.assertEqual(code, 1)

            code, _, _ = run("update", "acme/sample", "--merge-policy", "always")
            self.assertNotEqual(code, 0)

            self.assertEqual(_store(home), before)

    def test_usage_mentions_update(self):
        with isolated_hermes_home():
            run = self._cli()
            code, out, _ = run()
            self.assertEqual(code, 0)
            self.assertIn("update", out)


class TestUpdateRepoTool(unittest.TestCase):
    def _tools(self) -> dict[str, dict[str, Any]]:
        from factonio.agent_tools import register_tools

        ctx = _FakeCtx()
        register_tools(ctx)
        return ctx.tools

    def test_tool_schema(self):
        tools = self._tools()
        self.assertIn("factonio_update_repo", tools)
        schema = tools["factonio_update_repo"]["schema"]
        props = schema["parameters"]["properties"]
        self.assertEqual(schema["parameters"]["required"], ["repo"])
        self.assertEqual(
            set(props),
            {"repo", "merge_policy", "max_fix_rounds", "default_branch", "design_provider", "design_approval"},
        )
        self.assertIn("inherit", props["design_provider"]["enum"])
        self.assertIn("inherit", props["design_approval"]["enum"])

    def test_tool_updates_only_supplied_fields_and_clears(self):
        with isolated_hermes_home() as home:
            _register(home)
            handler = self._tools()["factonio_update_repo"]["handler"]

            res = json.loads(handler({"repo": "acme/sample", "max_fix_rounds": 4, "design_provider": "off"}))
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["updated_fields"], ["design_provider", "max_fix_rounds"])
            stored = _store(home)["acme-sample"]
            self.assertEqual(stored["merge_policy"], "auto")
            self.assertEqual(stored["design_provider"], "off")

            res = json.loads(handler({"repo": "acme-sample", "design_provider": None}))
            self.assertTrue(res["ok"], res)
            self.assertNotIn("design_provider", _store(home)["acme-sample"])

            res = json.loads(handler({"repo": "acme/sample", "design_approval": "inherit", "merge_policy": "ask"}))
            self.assertTrue(res["ok"], res)
            self.assertEqual(_store(home)["acme-sample"]["merge_policy"], "ask")

    def test_tool_errors(self):
        with isolated_hermes_home() as home:
            _register(home)
            handler = self._tools()["factonio_update_repo"]["handler"]
            before = _store(home)
            for args in (
                {"repo": "acme/sample"},
                {"repo": "nope/missing", "merge_policy": "ask"},
                {"repo": "acme/sample", "max_fix_rounds": 0},
                {"repo": "acme/sample", "path": "/tmp/evil"},
                {"repo": "acme/sample", "merge_policy": "always"},
            ):
                with self.subTest(args=args):
                    res = json.loads(handler(args))
                    self.assertFalse(res["ok"])
                    self.assertTrue(res["error"])
            self.assertEqual(_store(home), before)


if __name__ == "__main__":
    unittest.main()
