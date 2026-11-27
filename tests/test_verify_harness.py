"""Clean-room tests for the repo-local verification harness (`harness.py`) and its wiring."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tests._env import PLUGIN_ROOT, isolated_hermes_home

if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=Factonio Test", "-c", "user.email=test@example.com",
         "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )
    return proc.stdout.strip()


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _commit_all(repo: Path, msg: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


def _make_repo(root: Path, name: str = "svc") -> Path:
    """A small Python service: FastAPI-style routes, argparse CLI, pyproject scripts, CI workflow."""
    repo = root / name
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _write(repo, "pyproject.toml", '[project]\nname = "svc"\n\n[project.scripts]\nsvc = "svc.cli:main"\n')
    _write(repo, "api/server.py", (
        "from fastapi import APIRouter\nrouter = APIRouter()\n\n"
        "@router.get(\"/items\")\ndef items():\n    return []\n\n"
        "@router.post('/items/{item_id}')\ndef create(item_id):\n    return {}\n"
    ))
    _write(repo, "svc/cli.py", (
        "import argparse\n\ndef main():\n    p = argparse.ArgumentParser()\n    sub = p.add_subparsers()\n"
        "    sub.add_parser(\"serve\")\n    sub.add_parser('sync')\n"
    ))
    _write(repo, "tests/test_basic.py", "import unittest\n")
    _write(repo, ".github/workflows/ci.yml", "jobs:\n  t:\n    steps:\n      - run: python3 -m unittest discover -s tests -q\n")
    _commit_all(repo, "initial")
    return repo


def _index_fingerprint(repo: Path) -> tuple[bytes, int]:
    index = repo / ".git" / "index"
    return index.read_bytes(), index.stat().st_mtime_ns


class TestDetectRepoProfile(unittest.TestCase):
    def test_python_routes_cli_and_workflow(self):
        from factonio.harness import detect_repo_profile

        with isolated_hermes_home() as home:
            repo = _make_repo(home)
            profile = detect_repo_profile(repo)
            self.assertIn("python", profile["stack"])
            self.assertIn("github-actions", profile["stack"])
            self.assertIn("pyproject.toml", profile["manifests"])
            self.assertEqual(profile["routes"], ["GET /items", "POST /items/{item_id}"])
            self.assertEqual(profile["route_sources"]["GET /items"], "api/server.py")
            self.assertEqual(profile["cli_commands"], ["serve", "svc", "sync"])
            self.assertEqual(profile["workflows"], [".github/workflows/ci.yml"])
            self.assertIn("python3 -m unittest discover -s tests -q", profile["commands"]["test"])

    def test_ci_drive_uses_verification_allowlist(self):
        from factonio.harness import _is_ci_verification_command, _phases_for, detect_repo_profile

        with isolated_hermes_home() as home:
            repo = _make_repo(home)
            _write(
                repo,
                ".github/workflows/ci.yml",
                "jobs:\n"
                "  t:\n"
                "    steps:\n"
                "      - run: git status\n"
                "      - run: export FOO=1\n"
                "      - run: uv sync --frozen\n"
                "      - run: pip install -e .\n"
                "      - run: python -m pip install ruff\n"
                "      - run: npm ci\n"
                "      - run: apt-get update\n"
                "      - run: dnf install -y ffmpeg\n"
                "      - run: python3 -W error -m compileall -q .\n"
                "      - run: uvx ruff check --select E9,F .\n"
                "      - run: node --check desktop/plugin.js\n"
                "      - run: python3 tools/check_repo_integrity.py\n"
                "      - run: npm run test:ci\n",
            )
            profile = detect_repo_profile(repo)
            drive = _phases_for(profile)["drive"]
            for expected in (
                "python3 -W error -m compileall -q .",
                "uvx ruff check --select E9,F .",
                "node --check desktop/plugin.js",
                "python3 tools/check_repo_integrity.py",
                "npm run test:ci",
            ):
                self.assertIn(expected, drive)
            for excluded in (
                "git status",
                "export FOO=1",
                "uv sync --frozen",
                "pip install -e .",
                "python -m pip install ruff",
                "npm ci",
                "apt-get update",
                "dnf install -y ffmpeg",
            ):
                self.assertNotIn(excluded, drive)
                self.assertFalse(_is_ci_verification_command(excluded))
            self.assertFalse(_is_ci_verification_command("uv run pip install requests"))
            self.assertTrue(_is_ci_verification_command("cargo test --workspace"))
            self.assertTrue(_is_ci_verification_command("go test ./..."))

    def test_node_rust_go_make_flask_express_detection(self):
        from factonio.harness import detect_repo_profile

        with isolated_hermes_home() as home:
            repo = home / "poly"
            _write(repo, "package.json", json.dumps({
                "name": "@acme/tool", "bin": {"acme": "bin/acme.js"},
                "scripts": {"build": "tsc", "test": "jest", "start": "node server.js"},
            }))
            _write(repo, "server.js", "app.get('/health', h)\nrouter.delete(\"/users/:id\", h)\n")
            _write(repo, "Cargo.toml", '[package]\nname = "rtool"\n\n[[bin]]\nname = "rbin"\n')
            _write(repo, "go.mod", "module example.com/x\n")
            _write(repo, "main.go", 'http.HandleFunc("/ping", ping)\nmux.HandleFunc("POST /jobs", jobs)\n')
            _write(repo, "Makefile", "build:\n\tgo build\ntest:\n\tgo test\n")
            _write(repo, "web/app.py", "@app.route('/login', methods=['GET', 'POST'])\ndef login():\n    pass\n")
            _write(repo, "node_modules/dep/index.js", "app.get('/ignored', h)\n")
            profile = detect_repo_profile(repo)  # not a git repo: bounded walk fallback
            for lang in ("node", "rust", "go", "make", "python"):
                self.assertIn(lang, profile["stack"])
            self.assertEqual(
                profile["routes"],
                ["ANY /ping", "DELETE /users/:id", "GET /health", "GET /login", "POST /jobs", "POST /login"],
            )
            self.assertEqual(profile["cli_commands"], ["acme", "rbin"])
            self.assertIn("npm run build", profile["commands"]["build"])
            self.assertIn("make test", profile["commands"]["test"])
            self.assertIn("cargo test", profile["commands"]["test"])

    def test_git_probes_use_no_optional_locks(self):
        from factonio import harness

        with isolated_hermes_home() as home:
            repo = _make_repo(home)
            harness.bootstrap_verify_harness(str(repo))
            calls: list[list[str]] = []
            real_run = subprocess.run

            def spy(cmd, *a, **kw):
                if isinstance(cmd, list) and cmd and cmd[0] == "git":
                    calls.append(list(cmd))
                return real_run(cmd, *a, **kw)

            with mock.patch.object(harness.subprocess, "run", side_effect=spy):
                harness.detect_repo_profile(repo)
                harness.audit_harness(str(repo))
                harness.check_harness(str(repo))
            self.assertTrue(calls)
            for cmd in calls:
                self.assertEqual(cmd[1], "--no-optional-locks", cmd)


class TestBootstrap(unittest.TestCase):
    def test_init_refuse_force_dry_run_and_trail(self):
        from factonio import harness
        from factonio.trail import query_decisions

        with isolated_hermes_home() as home:
            repo = _make_repo(home)
            head = _git(repo, "rev-parse", "HEAD")
            hooks = [
                {"name": "integrity", "kind": "invariant", "command": "true"},
                {"name": "screens", "kind": "visual_diff", "command": "false", "optional": True},
            ]

            dry = harness.bootstrap_verify_harness(str(repo), dry_run=True, hooks=hooks)
            self.assertTrue(dry["ok"])
            self.assertFalse(dry["written"])
            self.assertFalse((repo / ".factonio").exists())
            self.assertIn("## Launch", dry["verify_md_content"])

            res = harness.bootstrap_verify_harness(str(repo), hooks=hooks, tenant="t-h", card_id="t_card")
            self.assertTrue(res["written"])
            md = (repo / ".factonio" / "verify.md").read_text(encoding="utf-8")
            for section in ("Launch", "Doctor", "Drive", "Evidence", "Cleanup", "Feature Map", "Hooks"):
                self.assertIn(f"\n## {section}\n", md)
            self.assertLess(md.index("## Launch"), md.index("## Doctor"))
            self.assertLess(md.index("## Evidence"), md.index("## Cleanup"))
            self.assertIn("**integrity** (`invariant`, required): `true`", md)
            manifest = json.loads((repo / ".factonio" / "harness.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], 1)
            self.assertEqual(manifest["base_commit"], head)
            self.assertEqual(set(manifest["phases"]), {"launch", "doctor", "drive", "evidence", "cleanup"})
            self.assertEqual(manifest["discovered_surfaces"]["routes"], ["GET /items", "POST /items/{item_id}"])
            ids = {f["id"]: f for f in manifest["feature_map"]}
            self.assertEqual(ids["api"]["paths"], ["api/**"])
            self.assertEqual(ids["api"]["surface"], "HTTP API")
            self.assertEqual(ids["svc"]["surface"], "CLI")
            self.assertEqual([h["name"] for h in manifest["hooks"]], ["integrity", "screens"])

            rows = query_decisions(phase="harness")
            self.assertEqual(len(rows), 1)
            self.assertEqual(
                (rows[0]["decision"], rows[0]["result"], rows[0]["tenant"], rows[0]["card_id"]),
                ("bootstrap_verify", "written", "t-h", "t_card"),
            )

            (repo / ".factonio" / "verify.md").write_text("# custom\n", encoding="utf-8")
            refused = harness.bootstrap_verify_harness(str(repo))
            self.assertFalse(refused["ok"])
            self.assertTrue(refused["refused"])
            self.assertEqual((repo / ".factonio" / "verify.md").read_text(encoding="utf-8"), "# custom\n")
            self.assertEqual(len(query_decisions(phase="harness")), 1)

            forced = harness.bootstrap_verify_harness(str(repo), force=True)
            self.assertTrue(forced["written"] and forced["overwritten"] and forced["preserved_hooks"])
            self.assertIn("## Doctor", (repo / ".factonio" / "verify.md").read_text(encoding="utf-8"))
            again = json.loads((repo / ".factonio" / "harness.json").read_text(encoding="utf-8"))
            self.assertEqual([h["name"] for h in again["hooks"]], ["integrity", "screens"])
            self.assertEqual(query_decisions(phase="harness")[0]["result"], "overwritten")
            self.assertEqual(list((repo / ".factonio").glob("*.tmp")), [])

    def test_invalid_hooks_and_unknown_repo_raise(self):
        from factonio import harness

        with isolated_hermes_home() as home:
            repo = _make_repo(home)
            for bad in (
                [{"name": "x", "kind": "nope", "command": "true"}],
                [{"name": "", "kind": "custom", "command": "true"}],
                [{"name": "x", "kind": "custom", "command": ""}],
                [{"name": "x", "kind": "custom", "command": "a"}, {"name": "x", "kind": "custom", "command": "b"}],
                [{"name": "x", "kind": "custom", "command": "true", "optional": "yes"}],
                "not-a-list",
            ):
                with self.assertRaises(ValueError, msg=repr(bad)):
                    harness.bootstrap_verify_harness(str(repo), hooks=bad)  # type: ignore[arg-type]  # deliberately invalid shapes
            self.assertFalse((repo / ".factonio").exists())
            with self.assertRaises(ValueError):
                harness.bootstrap_verify_harness(str(home / "missing-repo"))

    def test_registered_repo_slug_resolves_to_checkout(self):
        from factonio import harness
        from factonio.repos import add_repo

        with isolated_hermes_home() as home:
            repo = _make_repo(home)
            add_repo("acme/svc", local_path=str(repo), clone=False, inspect_remote=False)
            res = harness.bootstrap_verify_harness("acme/svc")
            self.assertTrue(res["written"])
            self.assertEqual(res["repo"], "acme/svc")
            self.assertEqual(harness.show_harness("acme-svc")["manifest"]["repo"], "acme/svc")


class TestDiscoverCheckHooks(unittest.TestCase):
    HAND_MD = (
        "# Hand harness\n\n## Launch\n```bash\necho launch\n```\n\n## Doctor\n```bash\ntrue\n```\n\n"
        "## Drive\n```bash\necho drive\n```\n\n## Evidence\n- logs\n\n## Cleanup\n- none\n\n"
        "## Hooks\n- **lint** (`invariant`, required): `true`\n- **pixels** (`visual_diff`, optional): `false`\n"
    )

    def test_verify_md_only_harness_is_supported(self):
        from factonio.harness import check_harness, discover_harness, show_harness

        with isolated_hermes_home() as home:
            repo = _make_repo(home)
            _write(repo, ".factonio/verify.md", self.HAND_MD)
            info = discover_harness(repo)
            self.assertTrue(info["present"])
            self.assertEqual(info["mode"], "verify_md_only")
            self.assertIsNone(info["harness_json"])
            self.assertEqual(info["missing_sections"], [])
            self.assertEqual([(h["name"], h["kind"], h["optional"]) for h in info["hooks"]],
                             [("lint", "invariant", False), ("pixels", "visual_diff", True)])
            self.assertEqual(info["phases"]["doctor"], ["true"])

            chk = check_harness(str(repo), run_doctor=True, run_hooks=True)
            self.assertTrue(chk["ok"], chk["errors"])
            self.assertTrue(any("harness.json" in w for w in chk["warnings"]))
            self.assertTrue(any("pixels" in w for w in chk["warnings"]))
            self.assertTrue(chk["doctor"]["ok"])
            self.assertEqual(show_harness(str(repo))["verify_md_content"], self.HAND_MD)

    def test_check_reports_missing_sections_schema_and_dead_globs(self):
        from factonio.harness import bootstrap_verify_harness, check_harness

        with isolated_hermes_home() as home:
            repo = _make_repo(home)
            missing = check_harness(str(repo))
            self.assertFalse(missing["ok"])
            self.assertFalse(missing["present"])

            bootstrap_verify_harness(str(repo))
            self.assertTrue(check_harness(str(repo))["ok"])

            md_path = repo / ".factonio" / "verify.md"
            md_path.write_text(md_path.read_text(encoding="utf-8").replace("## Cleanup", "## Teardown"), encoding="utf-8")
            hj = repo / ".factonio" / "harness.json"
            manifest = json.loads(hj.read_text(encoding="utf-8"))
            manifest["schema_version"] = 2
            manifest["feature_map"].append({"id": "ghost", "paths": ["ghost/**"]})
            hj.write_text(json.dumps(manifest), encoding="utf-8")
            chk = check_harness(str(repo))
            self.assertFalse(chk["ok"])
            self.assertEqual(chk["missing_sections"], ["Cleanup"])
            self.assertTrue(any("schema_version" in e for e in chk["errors"]))
            self.assertEqual(chk["dead_globs"], [{"feature": "ghost", "glob": "ghost/**"}])

            hj.write_text("{not json", encoding="utf-8")
            self.assertTrue(any("not valid JSON" in e for e in check_harness(str(repo))["errors"]))

    def test_doctor_and_hook_failures(self):
        from factonio.harness import bootstrap_verify_harness, check_harness, run_harness_hooks

        with isolated_hermes_home() as home:
            repo = _make_repo(home)
            bootstrap_verify_harness(str(repo), hooks=[
                {"name": "ok", "kind": "invariant", "command": "echo fine"},
                {"name": "soft", "kind": "visual_diff", "command": "exit 3", "optional": True},
                {"name": "hard", "kind": "custom", "command": ["sh", "-c", "echo boom >&2; exit 2"]},
            ])
            res = run_harness_hooks(repo)
            self.assertFalse(res["ok"])
            self.assertEqual(res["failed_required"], ["hard"])
            by_name = {r["name"]: r for r in res["results"]}
            self.assertEqual((by_name["ok"]["exit_code"], by_name["ok"]["stdout"].strip()), (0, "fine"))
            self.assertEqual(by_name["soft"]["exit_code"], 3)
            self.assertIn("boom", by_name["hard"]["stderr"])
            self.assertEqual(len(res["warnings"]), 1)

            only_visual = run_harness_hooks(repo, kinds=["visual_diff"])
            self.assertTrue(only_visual["ok"])
            self.assertEqual(only_visual["ran"], 1)
            with self.assertRaises(ValueError):
                run_harness_hooks(repo, kinds=["bogus"])

            slow = run_harness_hooks(repo, hooks=[{"name": "slow", "kind": "custom", "command": "sleep 5"}], timeout=1)
            self.assertFalse(slow["ok"])
            self.assertTrue(slow["results"][0]["timed_out"])
            optional_slow = run_harness_hooks(
                repo, hooks=[{"name": "slow", "kind": "custom", "command": "sleep 5", "optional": True, "timeout": 1}]
            )
            self.assertTrue(optional_slow["ok"])

            chk = check_harness(str(repo), run_hooks=True)
            self.assertFalse(chk["ok"])
            self.assertTrue(any("'hard'" in e for e in chk["errors"]))

            hj = repo / ".factonio" / "harness.json"
            manifest = json.loads(hj.read_text(encoding="utf-8"))
            manifest["hooks"] = []
            manifest["phases"]["doctor"] = ["true", "exit 7"]
            hj.write_text(json.dumps(manifest), encoding="utf-8")
            doc = check_harness(str(repo), run_doctor=True)
            self.assertFalse(doc["ok"])
            self.assertEqual([r["exit_code"] for r in doc["doctor"]["results"]], [0, 7])


class TestAudit(unittest.TestCase):
    def test_missing_and_clean(self):
        from factonio.harness import audit_harness, bootstrap_verify_harness

        with isolated_hermes_home() as home:
            repo = _make_repo(home)
            missing = audit_harness(str(repo))
            self.assertEqual((missing["severity"], missing["drifted"]), ("missing", True))
            self.assertTrue(missing["recommendations"])

            bootstrap_verify_harness(str(repo))
            _commit_all(repo, "add harness")
            clean = audit_harness(str(repo))
            self.assertEqual((clean["severity"], clean["drifted"]), ("clean", False), clean)
            self.assertEqual(clean["base_source"], "harness.json base_commit")

    def test_drift_detection_is_read_only(self):
        from factonio.harness import audit_harness, bootstrap_verify_harness

        with isolated_hermes_home() as home:
            repo = _make_repo(home)
            bootstrap_verify_harness(str(repo))
            _commit_all(repo, "add harness")

            _write(repo, "api/server.py", (
                "from fastapi import APIRouter\nrouter = APIRouter()\n\n"
                "@router.get(\"/items\")\ndef items():\n    return []\n\n"
                "@router.delete(\"/items/{item_id}\")\ndef drop(item_id):\n    return {}\n"
            ))
            _write(repo, "svc/cli.py", (
                "import argparse\n\ndef main():\n    sub = argparse.ArgumentParser().add_subparsers()\n"
                "    sub.add_parser('serve')\n    sub.add_parser('export')\n"
            ))
            _write(repo, "worker/jobs.py", "def run():\n    pass\n")
            _commit_all(repo, "drift")
            _write(repo, "loose.py", "x = 1\n")  # uncommitted, untracked root file

            before_status = _git(repo, "status", "--porcelain")  # refreshes the index before fingerprinting
            before_head = _git(repo, "rev-parse", "HEAD")
            before_index = _index_fingerprint(repo)
            res = audit_harness(str(repo))
            self.assertEqual(_index_fingerprint(repo), before_index)
            self.assertEqual(_git(repo, "rev-parse", "HEAD"), before_head)
            self.assertEqual(_git(repo, "status", "--porcelain"), before_status)

            self.assertTrue(res["drifted"])
            self.assertEqual(res["severity"], "stale")
            self.assertEqual(res["uncovered_changed_files"], ["loose.py", "worker/jobs.py"])
            self.assertEqual(sorted(res["stale_features"]), ["api", "svc"])
            self.assertEqual(res["new_routes"], ["DELETE /items/{item_id}"])
            self.assertEqual(res["removed_routes"], ["POST /items/{item_id}"])
            self.assertEqual(res["new_cli_commands"], ["export"])
            self.assertEqual(res["removed_cli_commands"], ["sync"])
            self.assertEqual(res["orphaned_globs"], [])
            self.assertTrue(res["recommendations"])
            self.assertNotIn(".factonio/harness.json", res["changed_files"])

    def test_orphaned_globs_and_explicit_base(self):
        from factonio.harness import audit_harness, bootstrap_verify_harness

        with isolated_hermes_home() as home:
            repo = _make_repo(home)
            first = _git(repo, "rev-parse", "HEAD")
            bootstrap_verify_harness(str(repo))
            _commit_all(repo, "add harness")
            _git(repo, "rm", "-q", "-r", "api")
            _commit_all(repo, "drop api")
            res = audit_harness(str(repo))
            self.assertEqual(res["orphaned_globs"], ["api/**"])
            self.assertEqual(res["removed_routes"], ["GET /items", "POST /items/{item_id}"])
            self.assertIn("api", res["stale_features"])

            head = _git(repo, "rev-parse", "HEAD")
            at_head = audit_harness(str(repo), base_ref=head)
            self.assertEqual(at_head["changed_files"], [])
            self.assertEqual(at_head["base_source"], "base_ref")
            self.assertEqual(audit_harness(str(repo), base_ref=first)["base_commit"], first)
            for bad in ("--output=x", "no-such-ref"):
                with self.assertRaises(ValueError):
                    audit_harness(str(repo), base_ref=bad)


class _FakeCtx:
    def __init__(self) -> None:
        self.cli: dict[str, Any] = {}
        self.tools: dict[str, dict[str, Any]] = {}
        self.blueprints: dict[str, dict[str, Any]] = {}

    def register_cli_command(self, name: str, *, help: str, setup_fn: Any, handler_fn: Any) -> None:
        self.cli[name] = {"setup_fn": setup_fn, "handler_fn": handler_fn}

    def register_tool(self, *, name: str, toolset: str, schema: dict, handler: Any) -> None:
        self.tools[name] = {"schema": schema, "handler": handler}

    def register_automation_blueprint(self, name: str, **kwargs: Any) -> None:
        self.blueprints[name] = kwargs

    def __getattr__(self, name: str) -> Any:
        return lambda *a, **kw: None


class TestSurfaces(unittest.TestCase):
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

    def test_cli_subcommands(self):
        with isolated_hermes_home() as home:
            repo = _make_repo(home)
            run = self._cli()

            code, out, _ = run("harness", "init", str(repo), "--dry-run")
            self.assertEqual(code, 0)
            self.assertIn("Would write", out)
            self.assertFalse((repo / ".factonio").exists())

            code, out, _ = run("harness", "init", str(repo), "--json")
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(out)["written"])
            code, out, _ = run("harness", "init", str(repo))
            self.assertEqual(code, 1)
            self.assertIn("Refused", out)
            code, _, _ = run("harness", "init", str(repo), "--force")
            self.assertEqual(code, 0)

            code, out, _ = run("harness", "check", str(repo), "--run-doctor", "--json")
            self.assertEqual(code, 0, out)
            self.assertTrue(json.loads(out)["doctor"]["ok"])
            code, out, _ = run("harness", "show", str(repo))
            self.assertEqual(code, 0)
            self.assertIn("## Feature Map", out)

            _commit_all(repo, "harness")
            code, out, _ = run("harness", "audit", str(repo), "--json")
            self.assertEqual((code, json.loads(out)["severity"]), (0, "clean"))
            _write(repo, "newmod/x.py", "y = 2\n")
            code, out, _ = run("harness", "audit", str(repo))
            self.assertEqual(code, 1)
            self.assertIn("STALE", out)
            self.assertIn("newmod/x.py", out)
            code, out, _ = run("harness", "audit", str(repo), "--base", "nope", "--json")
            self.assertEqual(code, 1)
            self.assertFalse(json.loads(out)["ok"])

    def test_tool_handler_and_schema(self):
        from factonio.agent_tools import register_tools

        with isolated_hermes_home() as home:
            repo = _make_repo(home)
            ctx = _FakeCtx()
            register_tools(ctx)
            tool = ctx.tools["factonio_bootstrap_verify"]
            props = tool["schema"]["parameters"]["properties"]
            self.assertEqual(props["action"]["enum"], ["init", "check", "show", "audit"])
            for key in ("repo", "force", "dry_run", "hooks", "base_ref", "run_doctor", "run_hooks"):
                self.assertIn(key, props)
            handler = tool["handler"]

            res = json.loads(handler({"repo": str(repo), "hooks": [{"name": "i", "kind": "invariant", "command": "true"}]}))
            self.assertEqual((res["action"], res["written"]), ("init", True))
            self.assertFalse(json.loads(handler({"repo": str(repo)}))["ok"])
            self.assertTrue(json.loads(handler({"action": "check", "repo": str(repo), "run_hooks": True}))["ok"])
            self.assertTrue(json.loads(handler({"action": "show", "repo": str(repo)}))["present"])
            self.assertEqual(json.loads(handler({"action": "audit", "repo": str(repo)}))["severity"], "clean")
            bad = json.loads(handler({"action": "explode", "repo": str(repo)}))
            self.assertFalse(bad["ok"])
            self.assertIn("action", bad["error"])

    def test_harness_audit_blueprint_registered(self):
        import factonio

        with isolated_hermes_home():
            ctx = _FakeCtx()
            factonio.register(ctx)
            self.assertIn("harness-audit", ctx.blueprints)
            self.assertIn("retro", ctx.blueprints)
            bp = ctx.blueprints["harness-audit"]
            self.assertIn("hermes factonio harness audit", bp["prompt_template"])
            self.assertIn("factonio_bootstrap_verify", ctx.tools)
            self.assertTrue(100 < len(factonio.ORCHESTRATOR_PROMPT_SECTION) < 3000)



class TestFixRound1Regressions(unittest.TestCase):
    """Bootstrap ID uniqueness, type-safe manifest validation, and the `valid` response contract."""

    MALFORMED: list[tuple[str, Any]] = [
        ("phases.doctor=17", lambda m: m["phases"].__setitem__("doctor", 17)),
        ("feature_map[0].id=['bad']", lambda m: m["feature_map"][0].__setitem__("id", ["bad"])),
        ("feature_map[0].paths=17", lambda m: m["feature_map"][0].__setitem__("paths", 17)),
        ("phases non-dict", lambda m: m.__setitem__("phases", ["launch"])),
        ("feature_map non-list", lambda m: m.__setitem__("feature_map", {"id": "x"})),
        ("hooks non-list", lambda m: m.__setitem__("hooks", {"name": "x"})),
        ("discovered_surfaces non-dict", lambda m: m.__setitem__("discovered_surfaces", 17)),
        ("discovered_surfaces.routes=17", lambda m: m["discovered_surfaces"].__setitem__("routes", 17)),
        ("phases.drive non-string item", lambda m: m["phases"].__setitem__("drive", ["true", 5])),
        ("feature_map[0].commands=17", lambda m: m["feature_map"][0].__setitem__("commands", 17)),
    ]

    def _bootstrap_check(self, files: dict[str, str]) -> tuple[list[str], dict[str, Any]]:
        from factonio.harness import bootstrap_verify_harness, check_harness

        with isolated_hermes_home() as home:
            repo = home / "collide"
            repo.mkdir()
            _git(repo, "init", "-q")
            for rel, text in files.items():
                _write(repo, rel, text)
            _commit_all(repo, "initial")
            res = bootstrap_verify_harness(str(repo))
            ids = [f["id"] for f in res["manifest"]["feature_map"]]
            return ids, check_harness(str(repo))

    def test_bootstrap_root_files_and_root_dir_get_unique_ids(self):
        ids, chk = self._bootstrap_check({"root.py": "a = 1\n", "root/x.py": "b = 2\n"})
        self.assertEqual(ids, ["root", "root-2"])
        self.assertEqual((chk["ok"], chk["valid"]), (True, True), chk["errors"])

    def test_bootstrap_colliding_directory_slugs_get_unique_ids(self):
        ids, chk = self._bootstrap_check({"api-v1/a.py": "a = 1\n", "api_v1/b.py": "b = 2\n", "main.py": "m = 0\n"})
        self.assertEqual(ids, ["root", "api-v1", "api-v1-2"])
        self.assertEqual(len(set(ids)), len(ids))
        self.assertEqual((chk["ok"], chk["valid"]), (True, True), chk["errors"])

    def test_malformed_manifest_types_return_structured_errors(self):
        from factonio.harness import audit_harness, bootstrap_verify_harness, check_harness, show_harness

        run = TestSurfaces._cli(self)
        with isolated_hermes_home() as home:
            repo = _make_repo(home)
            bootstrap_verify_harness(str(repo), hooks=[{"name": "ok", "kind": "invariant", "command": "true"}])
            _commit_all(repo, "harness")
            hj = repo / ".factonio" / "harness.json"
            pristine = json.loads(hj.read_text(encoding="utf-8"))
            for label, mutate in self.MALFORMED:
                with self.subTest(label):
                    manifest = json.loads(json.dumps(pristine))
                    mutate(manifest)
                    hj.write_text(json.dumps(manifest), encoding="utf-8")

                    chk = check_harness(str(repo), run_doctor=True, run_hooks=True)
                    self.assertEqual((chk["ok"], chk["valid"]), (False, False))
                    self.assertTrue(chk["errors"])
                    self.assertTrue(all(isinstance(e, str) and e for e in chk["errors"]))

                    code, out, err = run("harness", "check", str(repo), "--run-doctor", "--run-hooks", "--json")
                    self.assertNotEqual(code, 0)
                    self.assertNotIn("Traceback", out + err)
                    payload = json.loads(out)
                    self.assertEqual((payload["ok"], payload["valid"]), (False, False))
                    self.assertTrue(payload["errors"])

                    self.assertTrue(show_harness(str(repo))["present"])
                    audit = audit_harness(str(repo))
                    self.assertEqual((audit["severity"], audit["drifted"]), ("stale", True))
                    self.assertTrue(audit["manifest_errors"])
                    self.assertEqual(audit["drift_classification"]["classification"], "harness_gap")
                    self.assertEqual(chk["manifest_errors"], [e for e in chk["errors"] if e in chk["manifest_errors"]])
                    from factonio.harness import classify_harness_drift
                    self.assertEqual(classify_harness_drift(audit, chk)["classification"], "harness_gap")
                    for action in ("show", "audit"):
                        _, out, err = run("harness", action, str(repo), "--json")
                        self.assertNotIn("Traceback", out + err)
                        json.loads(out)

    def test_check_response_contract_includes_valid(self):
        from factonio.agent_tools import register_tools
        from factonio.harness import bootstrap_verify_harness, check_harness

        run = TestSurfaces._cli(self)
        with isolated_hermes_home() as home:
            repo = _make_repo(home)
            ctx = _FakeCtx()
            register_tools(ctx)
            handler = ctx.tools["factonio_bootstrap_verify"]["handler"]

            missing = check_harness(str(repo))
            self.assertEqual((missing["ok"], missing["valid"]), (False, False))
            code, out, _ = run("harness", "check", str(repo), "--json")
            self.assertEqual((code, json.loads(out)["ok"], json.loads(out)["valid"]), (1, False, False))

            bootstrap_verify_harness(str(repo))
            chk = check_harness(str(repo), run_doctor=True)
            self.assertEqual((chk["ok"], chk["valid"]), (True, True), chk["errors"])
            code, out, _ = run("harness", "check", str(repo), "--run-doctor", "--run-hooks", "--json")
            self.assertEqual((code, json.loads(out)["ok"], json.loads(out)["valid"]), (0, True, True))
            tool = json.loads(handler({"action": "check", "repo": str(repo)}))
            self.assertEqual((tool["ok"], tool["valid"]), (True, True))

            hj = repo / ".factonio" / "harness.json"
            manifest = json.loads(hj.read_text(encoding="utf-8"))
            manifest["schema_version"] = 99
            hj.write_text(json.dumps(manifest), encoding="utf-8")
            chk = check_harness(str(repo))
            self.assertEqual((chk["ok"], chk["valid"]), (False, False))
            code, out, _ = run("harness", "check", str(repo), "--json")
            self.assertEqual((code, json.loads(out)["ok"], json.loads(out)["valid"]), (1, False, False))


@unittest.skipUnless(os.name == "posix", "process-group cleanup is POSIX-only")
class TestTimeoutKillsDescendants(unittest.TestCase):
    """A timed-out compound shell command must not leave descendants that write after return."""

    def _compound(self, work: Path) -> tuple[str, Path, Path]:
        pidfile, marker = work / "child.pid", work / "late.txt"
        script = (
            "import os, pathlib, time; "
            f"pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid())); "
            "time.sleep(2); "
            f"pathlib.Path({str(marker)!r}).write_text('late side effect')"
        )
        return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}; :", pidfile, marker

    def _assert_group_dead(self, res: dict[str, Any], elapsed: float, pidfile: Path, marker: Path) -> None:
        self.assertTrue(res["timed_out"])
        self.assertFalse(res["ok"])
        self.assertIsNone(res["exit_code"])
        self.assertLess(elapsed, 4.0)
        self.assertFalse(marker.exists(), "marker written before return")
        child_pid = int(pidfile.read_text())
        time.sleep(2.5)
        self.assertFalse(marker.exists(), "descendant wrote a late side effect after timeout")
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)

    def test_hook_timeout_kills_shell_descendants(self):
        from factonio.harness import run_harness_hooks

        with isolated_hermes_home() as home:
            command, pidfile, marker = self._compound(home)
            started = time.monotonic()
            res = run_harness_hooks(home, hooks=[{"name": "slow", "kind": "custom", "command": command}], timeout=1)
            elapsed = time.monotonic() - started
            self.assertFalse(res["ok"])
            self._assert_group_dead(res["results"][0], elapsed, pidfile, marker)

    def test_doctor_timeout_kills_shell_descendants(self):
        from factonio import harness

        with isolated_hermes_home() as home:
            command, pidfile, marker = self._compound(home)
            started = time.monotonic()
            doc = harness._run_doctor(home, [command], timeout=1)
            elapsed = time.monotonic() - started
            self.assertFalse(doc["ok"])
            self._assert_group_dead(doc["results"][0], elapsed, pidfile, marker)

    def test_argv_timeout_and_normal_output_still_work(self):
        from factonio.harness import _run_command

        with isolated_hermes_home() as home:
            res = _run_command([sys.executable, "-c", "import time; time.sleep(5)"], home, 1)
            self.assertTrue(res["timed_out"])
            self.assertIn("Timed out after 1s", res["stderr"])
            ok = _run_command("echo hi; echo err >&2; exit 3", home, 10)
            self.assertEqual((ok["exit_code"], ok["stdout"].strip(), ok["stderr"].strip(), ok["timed_out"]),
                             (3, "hi", "err", False))
            missing = _run_command(["/nonexistent/factonio-binary"], home, 5)
            self.assertEqual((missing["exit_code"], missing["ok"]), (127, False))


class TestTestDirsExcludedFromSurfaces(unittest.TestCase):
    def test_test_directory_fixtures_never_become_routes_or_cli_commands(self):
        from factonio.harness import _TEST_DIRS, bootstrap_verify_harness, check_harness, detect_repo_profile

        self.assertEqual(_TEST_DIRS, frozenset({"tests", "test", "spec", "__tests__"}))
        with isolated_hermes_home() as home:
            repo = _make_repo(home)
            _write(repo, "tests/test_fixture.py", (
                "@router.get('/login')\ndef login():\n    pass\n\n"
                "@app.route('/legacy', methods=['POST'])\ndef legacy():\n    pass\n\n"
                "sub.add_parser('export')\n"
            ))
            _write(repo, "test/test_cli.py", "@cli.command('ghost')\ndef ghost():\n    pass\n")
            _write(repo, "spec/api.spec.js", "app.get('/spec-only', h)\n")
            _write(repo, "__tests__/server.test.ts", "router.post('/jest-only', h)\n")
            _write(repo, "lib/tests_helpers.py", "@router.get('/real')\ndef real():\n    pass\n")
            _commit_all(repo, "fixtures")

            profile = detect_repo_profile(repo)
            self.assertEqual(profile["routes"], ["GET /items", "GET /real", "POST /items/{item_id}"])
            self.assertEqual(profile["cli_commands"], ["serve", "svc", "sync"])
            for rel in ("tests/test_fixture.py", "test/test_cli.py", "spec/api.spec.js", "__tests__/server.test.ts"):
                self.assertIn(rel, profile["source_files"])

            res = bootstrap_verify_harness(str(repo))
            surfaces = res["manifest"]["discovered_surfaces"]
            self.assertEqual(surfaces["routes"], profile["routes"])
            self.assertEqual(surfaces["cli_commands"], profile["cli_commands"])
            by_glob = {f["paths"][0]: f for f in res["manifest"]["feature_map"]}
            for glob in ("tests/**", "test/**", "spec/**", "__tests__/**"):
                self.assertEqual(by_glob[glob]["surface"], "Tests")
                self.assertEqual(by_glob[glob]["scenarios"], [])
            self.assertEqual(by_glob["lib/**"]["surface"], "HTTP API")
            self.assertTrue(check_harness(str(repo))["valid"])


def _copy_tracked_checkout(src: Path, dest: Path) -> Path:
    """Snapshot the checkout's tracked + untracked (non-ignored) files into a fresh single-commit repo."""
    listed = subprocess.run(
        ["git", "--no-optional-locks", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=str(src), capture_output=True, text=True, check=False,
    )
    if listed.returncode != 0:
        raise unittest.SkipTest(f"{src} is not a git checkout: {listed.stderr.strip()}")
    dest.mkdir(parents=True)
    for rel in filter(None, listed.stdout.split("\0")):
        path = src / rel
        if not path.is_file():
            continue  # deleted-but-unstaged entries
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest / rel)
    _git(dest, "init", "-q")
    _commit_all(dest, "snapshot")
    return dest


class TestRepoDogfoodHarness(unittest.TestCase):
    """The committed `.factonio/` harness for this repository stays valid, complete, and drift-free."""

    REQUIRED_HOOKS = ("compileall", "ruff-static", "desktop-syntax", "repo-integrity")

    def _manifest(self) -> dict[str, Any]:
        path = PLUGIN_ROOT / ".factonio" / "harness.json"
        self.assertTrue(path.is_file(), "omnim-ai/factonio must commit .factonio/harness.json")
        self.assertTrue((PLUGIN_ROOT / ".factonio" / "verify.md").is_file())
        return json.loads(path.read_text(encoding="utf-8"))

    def test_manifest_shape_hooks_and_drive_wrapper(self):
        from factonio.harness import REQUIRED_SECTIONS, discover_harness

        manifest = self._manifest()
        self.assertEqual(manifest["schema_version"], 1)
        self.assertIsNone(manifest["base_commit"])
        self.assertEqual(manifest["repo"], "omnim-ai/factonio")
        hooks = {h["name"]: h for h in manifest["hooks"]}
        for name in self.REQUIRED_HOOKS:
            self.assertEqual(hooks[name]["kind"], "invariant")
            self.assertFalse(hooks[name]["optional"])
        drive = "\n".join(manifest["phases"]["drive"])
        for needle in ("mktemp -d", 'ln -s "$PWD" "$d/factonio"', "env -u HERMES_DELEGATED_CHILD_CONTEXT",
                       "python3 -m unittest discover -s tests"):
            self.assertIn(needle, drive)

        info = discover_harness(PLUGIN_ROOT)
        self.assertEqual(info["mode"], "full")
        self.assertEqual(info["missing_sections"], [])
        for section in (*REQUIRED_SECTIONS, "Feature Map", "Hooks"):
            self.assertIn(section, info["sections"])

    def test_feature_map_and_surfaces_match_live_repository(self):
        from factonio.harness import _any_match, detect_repo_profile

        manifest = self._manifest()
        profile = detect_repo_profile(PLUGIN_ROOT)
        surfaces = manifest["discovered_surfaces"]
        self.assertEqual(surfaces["routes"], profile["routes"])
        self.assertEqual(surfaces["cli_commands"], profile["cli_commands"])
        for src in [*profile["route_sources"].values(), *profile["cli_sources"].values()]:
            self.assertFalse(src.startswith(("tests/", "test/", "spec/", "__tests__/")), src)
        self.assertIn("PATCH /repos/{slug}", surfaces["routes"])
        self.assertIn("harness", surfaces["cli_commands"])

        globs = [g for feat in manifest["feature_map"] for g in feat["paths"]]
        uncovered = [rel for rel in profile["source_files"] if not _any_match(globs, rel)]
        self.assertEqual(uncovered, [])
        for rel in ("plugin.yaml", "dashboard/manifest.json", "skills/verify-blackbox/SKILL.md",
                    ".agents/skills/update-readme-media/SKILL.md", "CHANGELOG.md"):
            self.assertTrue(_any_match(globs, rel), rel)
        surfaces_by_id = {f["id"]: f["surface"] for f in manifest["feature_map"]}
        self.assertEqual(surfaces_by_id["dashboard-api"], "HTTP API")
        self.assertEqual(surfaces_by_id["cli"], "CLI")
        self.assertEqual(surfaces_by_id["desktop-ui"], "Desktop UI")
        self.assertEqual(surfaces_by_id["tests"], "Tests")

    @unittest.skipUnless(
        all(shutil.which(tool) for tool in ("git", "node", "uv", "uvx")),
        "running the harness hooks needs git, node, uv and uvx on PATH (provided in CI)",
    )
    def test_check_with_doctor_and_hooks_and_read_only_clean_audit(self):
        from factonio.harness import audit_harness, check_harness

        with isolated_hermes_home() as home:
            repo = _copy_tracked_checkout(PLUGIN_ROOT, home / "dogfood" / "factonio")
            # Doctor runs `python3 -c "import hermes_cli"`; resolve `python3` to this interpreter.
            path = os.pathsep.join([str(Path(sys.executable).parent), os.environ.get("PATH", "")])
            with mock.patch.dict(os.environ, {"PATH": path}):
                chk = check_harness(str(repo), run_doctor=True, run_hooks=True)
            self.assertTrue(chk["valid"], chk["errors"])
            self.assertTrue(chk["ok"])
            self.assertEqual((chk["dead_globs"], chk["warnings"]), ([], []))
            self.assertTrue(chk["doctor"]["ok"], chk["doctor"])
            ran = {r["name"]: r["ok"] for r in chk["hook_results"]["results"]}
            for name in self.REQUIRED_HOOKS:
                self.assertTrue(ran.get(name), chk["hook_results"])

            _git(repo, "status", "--porcelain")  # refresh the stat cache before fingerprinting
            before_index = _index_fingerprint(repo)
            audit = audit_harness(str(repo))
            self.assertEqual(_index_fingerprint(repo), before_index)
            self.assertEqual((audit["severity"], audit["drifted"]), ("clean", False), audit)
            self.assertEqual(audit["base_source"], "last commit touching .factonio/")
            for key in ("uncovered_changed_files", "orphaned_globs", "new_routes", "removed_routes",
                        "new_cli_commands", "removed_cli_commands", "stale_invariants", "dead_cover_globs",
                        "unresolved_sync_commits", "visual_baseline_changes"):
                self.assertEqual(audit[key], [], key)
            self.assertTrue(chk["formal_invariants"]["checked"])
            self.assertEqual(chk["formal_invariants"]["dead_cover_globs"], [])


def _fenced_block_after(text: str, marker: str) -> str:
    """Return the body of the first ```bash fenced block that follows ``marker``."""
    start = text.index(marker)
    open_at = text.index("```bash\n", start) + len("```bash\n")
    close_at = text.index("```", open_at)
    return "\n".join(line.strip() for line in text[open_at:close_at].splitlines())


class TestBlackboxIsolationRecipe(unittest.TestCase):
    """The verify-blackbox HERMES_HOME recipe must isolate Kanban paths even with an inherited home."""

    SENTINEL = "/sentinel-existing-home"
    PROBE = 'printf "%s\\n%s\\n%s\\n" "$HERMES_HOME" "$HERMES_KANBAN_DB" "$HERMES_KANBAN_WORKSPACES_ROOT"'

    def _recipe(self) -> str:
        text = (PLUGIN_ROOT / "skills" / "verify-blackbox" / "SKILL.md").read_text(encoding="utf-8")
        return _fenced_block_after(text, "Pin an isolated Hermes home")

    def test_recipe_resolves_kanban_paths_inside_new_home(self):
        recipe = self._recipe()
        for shell in ("bash", "sh"):
            if not shutil.which(shell):
                self.fail(f"{shell} is required to execute the documented recipe")
            with self.subTest(shell=shell), isolated_hermes_home() as scratch:
                tmpdir = scratch / "tmp"
                checkout = scratch / "checkout"
                tmpdir.mkdir()
                checkout.mkdir()
                env = {
                    **os.environ,
                    "TMPDIR": str(tmpdir),
                    "HERMES_HOME": self.SENTINEL,
                    "HERMES_KANBAN_DB": f"{self.SENTINEL}/kanban.db",
                    "HERMES_KANBAN_WORKSPACES_ROOT": f"{self.SENTINEL}/kanban/workspaces",
                }
                proc = subprocess.run(
                    [shell, "-c", f"set -e\n{recipe}\n{self.PROBE}"],
                    cwd=str(checkout), env=env, capture_output=True, text=True, timeout=30, check=False,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                home, db, ws_root = proc.stdout.splitlines()
                self.assertNotEqual(home, self.SENTINEL)
                self.assertTrue(Path(home).is_dir(), home)
                self.assertTrue(Path(home).resolve().is_relative_to(tmpdir.resolve()), home)
                self.assertEqual(db, f"{home}/kanban.db")
                self.assertEqual(ws_root, f"{home}/kanban/workspaces")
                for value in (db, ws_root):
                    self.assertFalse(value.startswith(self.SENTINEL), value)


class TestCommittedHarnessCliCommands(unittest.TestCase):
    """Every committed `hermes factonio ...` Launch/Drive command must parse with the real CLI parser."""

    READ_ONLY = ("models", "list")

    def _hermes_commands(self) -> list[tuple[str, list[str]]]:
        manifest = json.loads((PLUGIN_ROOT / ".factonio" / "harness.json").read_text(encoding="utf-8"))
        verify_md = (PLUGIN_ROOT / ".factonio" / "verify.md").read_text(encoding="utf-8")
        lines = [
            *(("harness.json", c) for phase in ("launch", "drive") for c in manifest["phases"][phase]),
            *(("verify.md", c) for marker in ("## Launch", "## Drive")
              for c in _fenced_block_after(verify_md, marker).splitlines()),
        ]
        found = []
        for source, line in lines:
            tokens = shlex.split(line)
            while tokens and "=" in tokens[0] and not tokens[0].startswith("-"):
                tokens.pop(0)
            if tokens[:2] == ["hermes", "factonio"]:
                found.append((source, tokens[2:]))
        return found

    def _cli(self):
        from factonio.commands import register_cli

        ctx = _FakeCtx()
        register_cli(ctx, PLUGIN_ROOT)
        entry = ctx.cli["factonio"]
        parser = argparse.ArgumentParser(prog="hermes factonio")
        entry["setup_fn"](parser)
        return parser, entry["handler_fn"]

    def test_every_committed_hermes_factonio_command_parses(self):
        parser, _ = self._cli()
        commands = self._hermes_commands()
        subcommands = {next((t for t in argv if not t.startswith("-")), None) for _, argv in commands}
        self.assertTrue({"harness", *self.READ_ONLY} <= subcommands, subcommands)
        for source, argv in commands:
            with self.subTest(source=source, argv=argv):
                err = io.StringIO()
                try:
                    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                        parser.parse_args(argv)
                except SystemExit as exc:
                    if exc.code not in (0, None):
                        self.fail(f"{source}: `hermes factonio {shlex.join(argv)}` exits {exc.code}: {err.getvalue()}")

    def test_read_only_drive_commands_exit_zero_with_json(self):
        parser, handler = self._cli()
        drive = [argv for source, argv in self._hermes_commands()
                 if source == "harness.json" and set(self.READ_ONLY) & set(argv)]
        self.assertEqual(len(drive), len(self.READ_ONLY), drive)
        for argv in drive:
            with self.subTest(argv=argv), isolated_hermes_home():
                out, err, code = io.StringIO(), io.StringIO(), 0
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    try:
                        handler(parser.parse_args(argv))
                    except SystemExit as exc:
                        code = 0 if exc.code is None else exc.code
                self.assertEqual(code, 0, err.getvalue())
                json.loads(out.getvalue())


class TestVerificationHarnessInjection(unittest.TestCase):
    def test_harness_only_reaches_blackbox_card(self):
        from hermes_cli import kanban_db as kb
        from hermes_cli.kanban_db_connect import connect_closing

        from factonio.harness import bootstrap_verify_harness
        from factonio.orchestrator import _format_harness_section, spawn_verification_pair
        from factonio.repos import add_repo

        with isolated_hermes_home() as home:
            repo = _make_repo(home)
            add_repo("acme/svc", local_path=str(repo), clone=False, inspect_remote=False)
            tenant = "acme-svc-h1"

            plain = spawn_verification_pair("acme/svc", tenant, pr_number=1, session_id="s")
            self.assertEqual((plain["harness_present"], plain["harness_path"]), (False, None))
            self.assertEqual(_format_harness_section(repo), "")
            with connect_closing() as conn:
                bb_plain = kb.get_task(conn, plain["verify_blackbox_task_id"])
            assert bb_plain is not None
            self.assertNotIn("Repo Verification Harness", bb_plain.body or "")

            bootstrap_verify_harness("acme/svc", hooks=[
                {"name": "pixels", "kind": "visual_diff", "command": "true", "optional": True},
            ])
            ver = spawn_verification_pair("acme/svc", tenant, pr_number=2, session_id="s")
            self.assertTrue(ver["harness_present"])
            self.assertEqual(ver["harness_path"], str(repo / ".factonio" / "verify.md"))
            with connect_closing() as conn:
                diff_task = kb.get_task(conn, ver["verify_diff_task_id"])
                bb_task = kb.get_task(conn, ver["verify_blackbox_task_id"])
            assert diff_task is not None and bb_task is not None
            bb_body = bb_task.body or ""
            self.assertIn("## Repo Verification Harness", bb_body)
            self.assertIn(str(repo / ".factonio" / "verify.md"), bb_body)
            self.assertIn(str(repo / ".factonio" / "harness.json"), bb_body)
            self.assertIn("Launch -> Doctor -> Drive -> Evidence -> Cleanup", bb_body)
            self.assertIn("`pixels` (visual_diff, optional)", bb_body)
            self.assertIn("NEVER run `git diff`", bb_body)
            self.assertNotIn(".factonio", diff_task.body or "")
            self.assertNotIn("Repo Verification Harness", diff_task.body or "")


_UNRAN_PROOF = {"ran": False, "ok": None, "doctor": None, "hooks": None, "evidence_survived": None, "errors": []}


@contextlib.contextmanager
def _scratch_tempdir(home: Path):
    """Route tempfile.mkdtemp into a private dir so proof temp roots can be counted exactly."""
    scratch = home / "proof-tmp"
    scratch.mkdir()
    with mock.patch.object(tempfile, "tempdir", str(scratch)):
        yield scratch


class TestHarnessProof(unittest.TestCase):
    def test_prove_false_reports_unran_proof_and_keeps_fields(self):
        from factonio import harness

        with isolated_hermes_home() as home:
            repo = _make_repo(home)
            dry = harness.bootstrap_verify_harness(str(repo), dry_run=True)
            self.assertEqual(dry["proof"], _UNRAN_PROOF)
            self.assertIn("verify_md_content", dry)
            res = harness.bootstrap_verify_harness(str(repo))
            self.assertEqual(res["proof"], _UNRAN_PROOF)
            for key in ("ok", "written", "refused", "existing", "would_overwrite", "overwritten", "preserved_hooks",
                        "manifest", "profile", "trail_decision_id"):
                self.assertIn(key, res)
            self.assertTrue(res["ok"] and res["written"])
            refused = harness.bootstrap_verify_harness(str(repo))
            self.assertTrue(refused["refused"])
            self.assertEqual(refused["proof"], _UNRAN_PROOF)

    def test_prove_success_writes_harness_and_cleans_proof_root(self):
        from factonio import harness
        from factonio.trail import query_decisions

        with isolated_hermes_home() as home, _scratch_tempdir(home) as scratch:
            repo = _make_repo(home)
            hooks = [{"name": "inv", "kind": "invariant", "command": "true"},
                     {"name": "soft", "kind": "visual_diff", "command": "exit 4", "optional": True}]
            res = harness.bootstrap_verify_harness(str(repo), hooks=hooks, prove=True, tenant="t-p", card_id="c1")
            self.assertTrue(res["ok"], res.get("error"))
            self.assertTrue(res["written"])
            proof = res["proof"]
            self.assertEqual((proof["ran"], proof["ok"], proof["evidence_survived"], proof["errors"]),
                             (True, True, True, []))
            self.assertTrue(proof["doctor"]["ok"])
            self.assertGreaterEqual(proof["doctor"]["ran"], 1)
            self.assertEqual((proof["hooks"]["ran"], proof["hooks"]["failed_required"]), (2, []))
            self.assertTrue((repo / ".factonio" / "harness.json").is_file())
            self.assertEqual(list(scratch.iterdir()), [])
            rows = query_decisions(phase="harness")
            self.assertEqual([r["result"] for r in rows], ["written"])
            self.assertTrue(rows[0]["evidence"]["proof"]["ok"])

    def test_prove_failure_on_fresh_repo_leaves_nothing_behind(self):
        from factonio import harness
        from factonio.trail import query_decisions

        with isolated_hermes_home() as home, _scratch_tempdir(home) as scratch:
            repo = _make_repo(home)
            res = harness.bootstrap_verify_harness(
                str(repo), hooks=[{"name": "gate", "kind": "invariant", "command": "exit 3"}], prove=True
            )
            self.assertFalse(res["ok"])
            self.assertFalse(res["written"])
            self.assertTrue(res["rolled_back"])
            self.assertIn("Harness proof failed", res["error"])
            self.assertEqual((res["proof"]["ran"], res["proof"]["ok"]), (True, False))
            self.assertEqual(res["proof"]["hooks"]["failed_required"], ["gate"])
            self.assertTrue(res["proof"]["evidence_survived"])
            self.assertTrue(any("'gate'" in e for e in res["proof"]["errors"]))
            self.assertFalse((repo / ".factonio").exists())
            self.assertEqual(_git(repo, "status", "--porcelain", "--untracked-files=all"), "")
            self.assertEqual(list(scratch.iterdir()), [])
            rows = query_decisions(phase="harness")
            self.assertEqual([r["result"] for r in rows], ["proof_failed"])
            self.assertEqual(rows[0]["evidence"]["proof"]["failed_required_hooks"], ["gate"])

    def test_prove_failure_restores_existing_harness_bytes(self):
        from factonio import harness

        with isolated_hermes_home() as home:
            repo = _make_repo(home)
            harness.bootstrap_verify_harness(str(repo), hooks=[{"name": "gate", "kind": "invariant", "command": "exit 1"}])
            (repo / ".factonio" / "notes.txt").write_text("keep\n", encoding="utf-8")
            before = {n: (repo / ".factonio" / n).read_bytes() for n in ("verify.md", "harness.json")}
            res = harness.bootstrap_verify_harness(str(repo), force=True, prove=True)
            self.assertFalse(res["ok"])
            self.assertFalse(res["written"])
            self.assertFalse(res["overwritten"])
            self.assertTrue(res["preserved_hooks"])
            for name, data in before.items():
                self.assertEqual((repo / ".factonio" / name).read_bytes(), data, name)
            self.assertEqual(sorted(p.name for p in (repo / ".factonio").iterdir()),
                             ["harness.json", "notes.txt", "verify.md"])

    def test_prove_failure_on_doctor_or_evidence_probe(self):
        from factonio import harness

        with isolated_hermes_home() as home, _scratch_tempdir(home) as scratch:
            repo = _make_repo(home)
            real_phases = harness._phases_for

            def broken_doctor(profile):
                phases = real_phases(profile)
                phases["doctor"] = [*phases["doctor"], "exit 9"]
                return phases

            with mock.patch.object(harness, "_phases_for", side_effect=broken_doctor):
                doc = harness.bootstrap_verify_harness(str(repo), prove=True)
            self.assertFalse(doc["ok"])
            self.assertFalse(doc["proof"]["doctor"]["ok"])
            self.assertTrue(any("exit 9" in e for e in doc["proof"]["errors"]))
            self.assertFalse((repo / ".factonio").exists())

            real_rmtree = shutil.rmtree

            def cleanup_destroys_evidence(path, *a, **kw):
                path = Path(path)
                if path.name == "work":
                    real_rmtree(path.parent / "evidence")
                return real_rmtree(path, *a, **kw)

            with mock.patch.object(harness.shutil, "rmtree", side_effect=cleanup_destroys_evidence):
                ev = harness.bootstrap_verify_harness(str(repo), prove=True)
            self.assertFalse(ev["ok"])
            self.assertFalse(ev["written"])
            self.assertIs(ev["proof"]["evidence_survived"], False)
            self.assertTrue(any("did not survive" in e for e in ev["proof"]["errors"]))
            self.assertFalse((repo / ".factonio").exists())
            self.assertEqual(list(scratch.iterdir()), [])

    def test_prove_dry_run_never_writes_or_logs(self):
        from factonio import harness
        from factonio.trail import query_decisions

        with isolated_hermes_home() as home:
            repo = _make_repo(home)
            ok = harness.bootstrap_verify_harness(str(repo), dry_run=True, prove=True)
            self.assertTrue(ok["ok"] and ok["proof"]["ok"])
            bad = harness.bootstrap_verify_harness(
                str(repo), dry_run=True, prove=True, hooks=[{"name": "g", "kind": "custom", "command": "false"}]
            )
            self.assertFalse(bad["ok"])
            self.assertFalse(bad["written"])
            self.assertFalse((repo / ".factonio").exists())
            self.assertEqual(query_decisions(phase="harness"), [])

    def test_cli_and_tool_prove(self):
        from factonio import harness
        from factonio.agent_tools import register_tools

        with isolated_hermes_home() as home:
            repo = _make_repo(home)
            run = TestSurfaces()._cli()
            code, out, _ = run("harness", "init", str(repo), "--prove", "--json")
            self.assertEqual(code, 0, out)
            payload = json.loads(out)
            self.assertTrue(payload["proof"]["ok"] and payload["written"])

            harness.bootstrap_verify_harness(
                str(repo), force=True, hooks=[{"name": "gate", "kind": "invariant", "command": "exit 2"}]
            )
            before = (repo / ".factonio" / "harness.json").read_bytes()
            code, out, _ = run("harness", "init", str(repo), "--force", "--prove")
            self.assertEqual(code, 1)
            self.assertIn("Proof FAILED", out)
            self.assertIn("'gate'", out)
            self.assertEqual((repo / ".factonio" / "harness.json").read_bytes(), before)

            ctx = _FakeCtx()
            register_tools(ctx)
            tool = ctx.tools["factonio_bootstrap_verify"]
            self.assertEqual(tool["schema"]["parameters"]["properties"]["prove"]["type"], "boolean")
            res = json.loads(tool["handler"]({"repo": str(repo), "force": True, "prove": True}))
            self.assertFalse(res["ok"])
            self.assertFalse(res["written"])
            self.assertEqual(res["proof"]["hooks"]["failed_required"], ["gate"])
            self.assertEqual((repo / ".factonio" / "harness.json").read_bytes(), before)


class TestGateLoosening(unittest.TestCase):
    HOOKS = [
        {"name": "lint", "kind": "invariant", "command": "true"},
        {"name": "types", "kind": "invariant", "command": "true"},
        {"name": "pixels", "kind": "visual_diff", "command": "true", "optional": True},
    ]

    def _repo_with_committed_harness(self, home: Path) -> tuple[Path, str]:
        from factonio.harness import bootstrap_verify_harness

        repo = _make_repo(home)
        bootstrap_verify_harness(str(repo), hooks=self.HOOKS)
        return repo, _commit_all(repo, "add harness")

    @staticmethod
    def _loosen(repo: Path) -> None:
        hj = repo / ".factonio" / "harness.json"
        manifest = json.loads(hj.read_text(encoding="utf-8"))
        manifest["hooks"] = [h for h in manifest["hooks"] if h["name"] != "lint"]
        for h in manifest["hooks"]:
            if h["name"] == "types":
                h["optional"] = True
        manifest["phases"]["doctor"] = [c for c in manifest["phases"]["doctor"] if c != "git --version"]
        hj.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    def test_clean_and_loosened_against_head_is_read_only(self):
        from factonio.harness import detect_harness_gate_loosening

        with isolated_hermes_home() as home:
            repo, head = self._repo_with_committed_harness(home)
            clean = detect_harness_gate_loosening(repo)
            self.assertEqual((clean["checked"], clean["loosened"], clean["base_commit"]), (True, False, head))
            self.assertEqual(clean["baseline_source"], "harness.json")
            self.assertEqual(clean["findings"], [])

            self._loosen(repo)
            before_status = _git(repo, "status", "--porcelain")
            before_index = _index_fingerprint(repo)
            res = detect_harness_gate_loosening(repo)
            self.assertEqual(_index_fingerprint(repo), before_index)
            self.assertEqual(_git(repo, "status", "--porcelain"), before_status)
            self.assertEqual(set(res), {"checked", "loosened", "base_ref", "base_commit", "baseline_source",
                                        "removed_required_hooks", "downgraded_hooks", "removed_doctor_commands",
                                        "loosened_visual_hooks", "visual_baseline_changes", "findings"})
            self.assertTrue(res["checked"] and res["loosened"])
            self.assertEqual(res["removed_required_hooks"], ["lint"])
            self.assertEqual(res["downgraded_hooks"], ["types"])
            self.assertEqual(res["removed_doctor_commands"], ["git --version"])
            self.assertEqual(len(res["findings"]), 3)

    def test_optional_hook_removal_and_added_gates_are_not_loosening(self):
        from factonio.harness import detect_harness_gate_loosening

        with isolated_hermes_home() as home:
            repo, _ = self._repo_with_committed_harness(home)
            hj = repo / ".factonio" / "harness.json"
            manifest = json.loads(hj.read_text(encoding="utf-8"))
            manifest["hooks"] = [h for h in manifest["hooks"] if h["name"] != "pixels"]
            manifest["hooks"].append({"name": "extra", "kind": "custom", "command": "true", "optional": False})
            manifest["phases"]["doctor"].append("uname")
            hj.write_text(json.dumps(manifest), encoding="utf-8")
            res = detect_harness_gate_loosening(repo)
            self.assertTrue(res["checked"])
            self.assertFalse(res["loosened"], res["findings"])

    def test_check_and_audit_fail_closed_when_loosened(self):
        from factonio.harness import audit_harness, check_harness

        with isolated_hermes_home() as home:
            repo, _ = self._repo_with_committed_harness(home)
            ok = check_harness(str(repo))
            self.assertTrue(ok["ok"], ok["errors"])
            self.assertFalse(ok["gate_loosening"]["loosened"])
            audit_ok = audit_harness(str(repo))
            self.assertEqual((audit_ok["severity"], audit_ok["drifted"]), ("clean", False))
            self.assertTrue(audit_ok["gate_loosening"]["checked"])
            self.assertEqual(audit_ok["drift_classification"]["classification"], "clean")

            self._loosen(repo)
            chk = check_harness(str(repo))
            self.assertEqual((chk["ok"], chk["valid"]), (False, False))
            self.assertTrue(chk["gate_loosening"]["loosened"])
            for needle in ("'lint'", "'types'", "git --version"):
                self.assertTrue(any(needle in e and "Gate loosening" in e for e in chk["errors"]), needle)

            audit = audit_harness(str(repo))
            self.assertEqual((audit["severity"], audit["drifted"]), ("stale", True))
            self.assertEqual(audit["gate_loosening"]["removed_required_hooks"], ["lint"])
            self.assertTrue(any("Gate loosening" in w for w in audit["warnings"]))
            self.assertTrue(any("Restore the removed or downgraded" in r for r in audit["recommendations"]))
            self.assertEqual(audit["drift_classification"]["classification"], "doc_drift")

    def test_explicit_base_ref_catches_committed_loosening(self):
        from factonio.harness import check_harness, detect_harness_gate_loosening

        with isolated_hermes_home() as home:
            repo, harness_commit = self._repo_with_committed_harness(home)
            self._loosen(repo)
            _commit_all(repo, "loosen gates")
            self.assertFalse(detect_harness_gate_loosening(repo)["loosened"])
            self.assertTrue(check_harness(str(repo))["ok"])
            res = detect_harness_gate_loosening(repo, base_ref=harness_commit)
            self.assertEqual((res["loosened"], res["base_ref"], res["base_commit"]), (True, harness_commit, harness_commit))
            chk = check_harness(str(repo), base_ref=harness_commit)
            self.assertFalse(chk["valid"])
            for bad in ("--output=x", "no-such-ref", ""):
                with self.assertRaises(ValueError):
                    detect_harness_gate_loosening(repo, base_ref=bad)
            with self.assertRaises(ValueError):
                check_harness(str(repo), base_ref="no-such-ref")

            run = TestSurfaces()._cli()
            code, out, _ = run("harness", "check", str(repo), "--base", harness_commit, "--json")
            self.assertEqual(code, 1)
            self.assertTrue(json.loads(out)["gate_loosening"]["loosened"])
            code, out, _ = run("harness", "check", str(repo), "--json")
            self.assertEqual(code, 0, out)
            code, out, _ = run("harness", "check", str(repo), "--base", "no-such-ref", "--json")
            self.assertEqual(code, 1)
            self.assertFalse(json.loads(out)["ok"])

            from factonio.agent_tools import register_tools

            ctx = _FakeCtx()
            register_tools(ctx)
            handler = ctx.tools["factonio_bootstrap_verify"]["handler"]
            res = json.loads(handler({"action": "check", "repo": str(repo), "base_ref": harness_commit}))
            self.assertFalse(res["valid"])
            self.assertEqual(res["gate_loosening"]["downgraded_hooks"], ["types"])

    def test_verify_md_baseline_fallback_and_unchecked_cases(self):
        from factonio.harness import detect_harness_gate_loosening

        with isolated_hermes_home() as home:
            repo = _make_repo(home)
            _write(repo, ".factonio/verify.md", TestDiscoverCheckHooks.HAND_MD)
            unchecked = detect_harness_gate_loosening(repo)
            self.assertEqual((unchecked["checked"], unchecked["loosened"]), (False, False))
            _commit_all(repo, "hand harness")
            md = repo / ".factonio" / "verify.md"
            md.write_text(
                TestDiscoverCheckHooks.HAND_MD.replace("- **lint** (`invariant`, required): `true`\n", "")
                .replace("```bash\ntrue\n```\n\n## Drive", "```bash\n```\n\n## Drive"),
                encoding="utf-8",
            )
            res = detect_harness_gate_loosening(repo)
            self.assertEqual(res["baseline_source"], "verify.md")
            self.assertEqual(res["removed_required_hooks"], ["lint"])
            self.assertEqual(res["removed_doctor_commands"], ["true"])

            plain = home / "plain"
            plain.mkdir()
            self.assertFalse(detect_harness_gate_loosening(plain)["checked"])
            with self.assertRaises(ValueError):
                detect_harness_gate_loosening(plain, base_ref="HEAD")


class TestDriftClassification(unittest.TestCase):
    @staticmethod
    def _hook(name: str, *, ok: bool, exit_code: Any, optional: bool = False, timed_out: bool = False) -> dict:
        return {"name": name, "kind": "invariant", "optional": optional, "ok": ok, "exit_code": exit_code,
                "timed_out": timed_out, "command": "x"}

    def test_priority_and_shape(self):
        from factonio.harness import classify_harness_drift

        clean = classify_harness_drift({"severity": "clean"})
        self.assertEqual(set(clean), {"classification", "classifications", "reasons", "recommended_action"})
        self.assertEqual((clean["classification"], clean["classifications"], clean["reasons"]), ("clean", [], []))
        self.assertEqual(classify_harness_drift(None)["classification"], "clean")

        stale = classify_harness_drift({"severity": "stale", "uncovered_changed_files": ["a.py"]})
        self.assertEqual(stale["classification"], "doc_drift")
        self.assertIn(".factonio/", stale["recommended_action"])
        self.assertEqual(classify_harness_drift({"severity": "missing"})["classification"], "doc_drift")

        doctor_fail = {"doctor": {"ok": False, "results": [{"command": "node --version", "ok": False, "exit_code": 127}]}}
        gap = classify_harness_drift({"severity": "stale", "stale_features": ["api"]}, doctor_fail)
        self.assertEqual((gap["classification"], gap["classifications"]), ("harness_gap", ["harness_gap", "doc_drift"]))

        for hook in (self._hook("h", ok=False, exit_code=127), self._hook("h", ok=False, exit_code=None, timed_out=True)):
            res = classify_harness_drift({"severity": "clean"}, {"hook_results": {"results": [hook]}})
            self.assertEqual(res["classification"], "harness_gap", hook)

        regression = classify_harness_drift(
            {"severity": "stale", "orphaned_globs": ["x/**"]},
            {**doctor_fail, "hook_results": {"results": [self._hook("tests", ok=False, exit_code=1)]}},
        )
        self.assertEqual(regression["classification"], "product_regression")
        self.assertEqual(regression["classifications"], ["product_regression", "harness_gap", "doc_drift"])
        self.assertTrue(regression["reasons"][0].startswith("product_regression: "))
        self.assertIn("never edit `.factonio/`", regression["recommended_action"])

        optional_only = classify_harness_drift(
            {"severity": "clean"}, {"hook_results": {"results": [self._hook("pix", ok=False, exit_code=1, optional=True)]}}
        )
        self.assertEqual(optional_only["classification"], "clean")
        schema = classify_harness_drift({"severity": "clean"}, {"errors": ["harness.json schema_version must be 1; got 2"]})
        self.assertEqual((schema["classification"], schema["classifications"]), ("harness_gap", ["harness_gap"]))
        structured = classify_harness_drift({"severity": "stale", "manifest_errors": ["feature_map has duplicate id 'x'"]})
        self.assertEqual(structured["classification"], "harness_gap")
        self.assertTrue(structured["reasons"][0].startswith("harness_gap: Malformed .factonio/harness.json"))
        outranked = classify_harness_drift(
            {"severity": "stale", "manifest_errors": ["harness.json is not valid JSON: x"]},
            {"hook_results": {"results": [self._hook("tests", ok=False, exit_code=1)]}},
        )
        self.assertEqual(outranked["classifications"], ["product_regression", "harness_gap"])

    def test_live_check_results_classify_end_to_end(self):
        from factonio.harness import audit_harness, bootstrap_verify_harness, check_harness, classify_harness_drift

        with isolated_hermes_home() as home:
            repo = _make_repo(home)
            bootstrap_verify_harness(str(repo), hooks=[
                {"name": "tests", "kind": "invariant", "command": "exit 1"},
                {"name": "tool", "kind": "custom", "command": "definitely-not-a-real-binary-xyz"},
            ])
            _commit_all(repo, "harness")
            audit = audit_harness(str(repo))
            self.assertEqual(audit["drift_classification"]["classification"], "clean")
            chk = check_harness(str(repo), run_doctor=True, run_hooks=True)
            res = classify_harness_drift(audit, chk)
            self.assertEqual(res["classification"], "product_regression")
            self.assertEqual(res["classifications"], ["product_regression", "harness_gap"])
            self.assertTrue(any("'tests'" in r for r in res["reasons"]))
            self.assertTrue(any("'tool'" in r and "could not run" in r for r in res["reasons"]))

            _write(repo, "newpkg/mod.py", "x = 1\n")
            stale = audit_harness(str(repo))
            self.assertEqual(stale["drift_classification"]["classification"], "doc_drift")
            self.assertTrue(any(r.startswith("Drift classification `doc_drift`") for r in stale["recommendations"]))


class TestHybridBaselineAndMalformedManifest(unittest.TestCase):
    """ hybrid JSON/Markdown gate baselines and malformed manifests as `harness_gap`."""

    HOOKS = [
        {"name": "lint", "kind": "invariant", "command": "true"},
        {"name": "pixels", "kind": "visual_diff", "command": "true", "optional": True},
    ]
    LINT_LINE = "- **lint** (`invariant`, required): `true`"

    def _hybrid_repo(self, home: Path) -> Path:
        """A valid committed harness whose harness.json has no `hooks` key, so hooks come from verify.md."""
        from factonio.harness import bootstrap_verify_harness, check_harness

        repo = _make_repo(home)
        bootstrap_verify_harness(str(repo), hooks=self.HOOKS)
        hj = repo / ".factonio" / "harness.json"
        manifest = json.loads(hj.read_text(encoding="utf-8"))
        del manifest["hooks"]
        hj.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        self.assertIn(self.LINT_LINE, (repo / ".factonio" / "verify.md").read_text(encoding="utf-8"))
        _commit_all(repo, "hybrid harness")
        baseline = check_harness(str(repo), run_doctor=True, run_hooks=True)
        self.assertTrue(baseline["valid"], baseline["errors"])
        self.assertEqual([(h["name"], h["optional"]) for h in baseline["hooks"]], [("lint", False), ("pixels", True)])
        self.assertEqual(baseline["hook_results"]["ran"], 2)
        self.assertFalse(baseline["gate_loosening"]["loosened"])
        return repo

    @staticmethod
    def _edit_md(repo: Path, old: str, new: str) -> None:
        md = repo / ".factonio" / "verify.md"
        text = md.read_text(encoding="utf-8")
        assert old in text, old
        md.write_text(text.replace(old, new, 1), encoding="utf-8")

    def _assert_loosened(self, repo: Path, field: str, expected: list[str]) -> None:
        from factonio.harness import audit_harness, check_harness, detect_harness_gate_loosening

        before_index = _index_fingerprint(repo)
        gate = detect_harness_gate_loosening(repo)
        self.assertEqual((gate["checked"], gate["loosened"]), (True, True), gate)
        self.assertEqual(gate["baseline_source"], "harness.json")
        self.assertEqual(gate[field], expected)
        chk = check_harness(str(repo), run_doctor=True, run_hooks=True)
        self.assertEqual((chk["ok"], chk["valid"]), (False, False))
        self.assertTrue(chk["gate_loosening"]["loosened"])
        self.assertTrue(any(e.startswith("Gate loosening") for e in chk["errors"]), chk["errors"])
        audit = audit_harness(str(repo))
        self.assertEqual((audit["severity"], audit["drifted"]), ("stale", True))
        self.assertEqual(audit["gate_loosening"][field], expected)
        self.assertEqual(audit["drift_classification"]["classification"], "doc_drift")
        self.assertEqual(_index_fingerprint(repo), before_index)

    def test_hybrid_baseline_required_md_hook_removed(self):
        with isolated_hermes_home() as home:
            repo = self._hybrid_repo(home)
            self._edit_md(repo, self.LINT_LINE + "\n", "")
            self._assert_loosened(repo, "removed_required_hooks", ["lint"])

    def test_hybrid_baseline_required_md_hook_downgraded(self):
        with isolated_hermes_home() as home:
            repo = self._hybrid_repo(home)
            self._edit_md(repo, self.LINT_LINE, self.LINT_LINE.replace("required", "optional"))
            self._assert_loosened(repo, "downgraded_hooks", ["lint"])

    def test_hybrid_baseline_moving_hook_into_json_is_not_loosening(self):
        from factonio.harness import check_harness

        with isolated_hermes_home() as home:
            repo = self._hybrid_repo(home)
            hj = repo / ".factonio" / "harness.json"
            manifest = json.loads(hj.read_text(encoding="utf-8"))
            manifest["hooks"] = [{"name": "lint", "kind": "invariant", "command": "true", "optional": False}]
            hj.write_text(json.dumps(manifest), encoding="utf-8")
            self._edit_md(repo, self.LINT_LINE + "\n", "")
            chk = check_harness(str(repo), run_doctor=True, run_hooks=True)
            self.assertTrue(chk["valid"], chk["errors"])
            self.assertFalse(chk["gate_loosening"]["loosened"])

            manifest["hooks"] = []
            hj.write_text(json.dumps(manifest), encoding="utf-8")
            chk = check_harness(str(repo))
            self.assertEqual(chk["gate_loosening"]["removed_required_hooks"], ["lint"])
            self.assertFalse(chk["valid"])

    def _assert_malformed_is_harness_gap(self, repo: Path, needle: str) -> None:
        from factonio.harness import audit_harness, check_harness, classify_harness_drift

        audit = audit_harness(str(repo))
        self.assertEqual((audit["severity"], audit["drifted"]), ("stale", True))
        self.assertTrue(any(needle in e for e in audit["manifest_errors"]), audit["manifest_errors"])
        self.assertEqual(audit["drift_classification"]["classification"], "harness_gap")
        self.assertNotIn("No action", audit["drift_classification"]["recommended_action"])
        self.assertTrue(any(r.startswith("Drift classification `harness_gap`") for r in audit["recommendations"]))

        chk = check_harness(str(repo), run_doctor=True, run_hooks=True)
        self.assertEqual((chk["ok"], chk["valid"]), (False, False))
        self.assertTrue(any(needle in e for e in chk["manifest_errors"]), chk["manifest_errors"])
        res = classify_harness_drift(audit, chk)
        self.assertEqual(res["classification"], "harness_gap")
        self.assertNotIn("doc_drift", res["classifications"])
        self.assertEqual(classify_harness_drift(None, chk)["classification"], "harness_gap")

        run = TestSurfaces()._cli()
        code, out, _ = run("harness", "audit", str(repo), "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["drift_classification"]["classification"], "harness_gap")

    def test_invalid_json_manifest_is_harness_gap(self):
        from factonio.harness import bootstrap_verify_harness

        with isolated_hermes_home() as home:
            repo = _make_repo(home)
            bootstrap_verify_harness(str(repo), hooks=self.HOOKS)
            _commit_all(repo, "harness")
            (repo / ".factonio" / "harness.json").write_text("{not json", encoding="utf-8")
            self._assert_malformed_is_harness_gap(repo, "not valid JSON")

    def test_unsupported_schema_version_is_harness_gap(self):
        from factonio.harness import bootstrap_verify_harness

        with isolated_hermes_home() as home:
            repo = _make_repo(home)
            bootstrap_verify_harness(str(repo), hooks=self.HOOKS)
            _commit_all(repo, "harness")
            hj = repo / ".factonio" / "harness.json"
            manifest = json.loads(hj.read_text(encoding="utf-8"))
            manifest["schema_version"] = 999
            hj.write_text(json.dumps(manifest), encoding="utf-8")
            self._assert_malformed_is_harness_gap(repo, "schema_version")

    def test_informational_audit_warnings_stay_clean(self):
        from factonio.harness import audit_harness

        with isolated_hermes_home() as home:
            repo = _make_repo(home)
            _write(repo, ".factonio/verify.md", TestDiscoverCheckHooks.HAND_MD)
            _commit_all(repo, "hand harness")
            audit = audit_harness(str(repo))
            self.assertTrue(audit["warnings"])
            self.assertEqual(audit["manifest_errors"], [])
            self.assertEqual((audit["severity"], audit["drifted"]), ("clean", False))
            self.assertEqual(audit["drift_classification"]["classification"], "clean")


class TestFormalInvariantHookMetadata(unittest.TestCase):
    """Optional `covers`/`sync_commit` (invariant) and `baseline_paths`/`frozen_baseline` (visual_diff) metadata."""

    def test_validate_accepts_normalizes_and_rejects_by_kind(self):
        from factonio.harness import validate_hooks

        hooks = validate_hooks([
            {"name": "model", "kind": "invariant", "command": "true", "covers": [" api/** ", "svc/*.py"],
             "sync_commit": " abc123 "},
            {"name": "pixels", "kind": "visual_diff", "command": "true", "baseline_paths": ["snapshots/**"]},
            {"name": "thawed", "kind": "visual_diff", "command": "true", "baseline_paths": ["s/**"], "frozen_baseline": False},
            {"name": "plain", "kind": "invariant", "command": "true", "covers": None},
        ])
        self.assertEqual((hooks[0]["covers"], hooks[0]["sync_commit"]), (["api/**", "svc/*.py"], "abc123"))
        self.assertEqual((hooks[1]["baseline_paths"], hooks[1]["frozen_baseline"]), (["snapshots/**"], True))
        self.assertIs(hooks[2]["frozen_baseline"], False)
        self.assertEqual(set(hooks[3]), {"name", "kind", "command", "optional"})

        bad = [
            {"kind": "custom", "covers": ["a/**"]},
            {"kind": "visual_diff", "sync_commit": "HEAD", "covers": ["a"]},
            {"kind": "invariant", "baseline_paths": ["s/**"]},
            {"kind": "custom", "frozen_baseline": True},
            {"kind": "invariant", "covers": []},
            {"kind": "invariant", "covers": ["ok", " "]},
            {"kind": "invariant", "covers": "a/**"},
            {"kind": "invariant", "sync_commit": "HEAD"},
            {"kind": "invariant", "covers": ["a"], "sync_commit": "--output=x"},
            {"kind": "invariant", "covers": ["a"], "sync_commit": "a b"},
            {"kind": "invariant", "covers": ["a"], "sync_commit": "a\x07"},
            {"kind": "invariant", "covers": ["a"], "sync_commit": "  "},
            {"kind": "invariant", "covers": ["a"], "sync_commit": 7},
            {"kind": "visual_diff", "baseline_paths": []},
            {"kind": "visual_diff", "baseline_paths": ["s/**"], "frozen_baseline": "yes"},
        ]
        for extra in bad:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                validate_hooks([{"name": "h", "command": "true", **extra}])

    def test_render_parse_round_trip_and_force_preserves_metadata(self):
        from factonio.harness import HOOK_KINDS, _HOOK_LINE_RE, bootstrap_verify_harness, discover_harness

        with isolated_hermes_home() as home:
            repo = _make_repo(home)
            _write(repo, "snapshots/home.png", "png-v1\n")
            hooks = [
                {"name": "model", "kind": "invariant", "command": "true", "covers": ["api/**"], "sync_commit": "HEAD"},
                {"name": "pixels", "kind": "visual_diff", "command": "true", "baseline_paths": ["snapshots/**"]},
            ]
            bootstrap_verify_harness(str(repo), hooks=hooks)
            md = (repo / ".factonio" / "verify.md").read_text(encoding="utf-8")
            self.assertIn("- **model** (`invariant`, required): `true` [covers: `api/**`; sync_commit: `HEAD`]", md)
            self.assertIn(
                "- **pixels** (`visual_diff`, required): `true` [baseline_paths: `snapshots/**`; frozen_baseline: `true`]", md
            )
            self.assertEqual([m.group("cmd") for m in _HOOK_LINE_RE.finditer(md)], ["true", "true"])
            self.assertEqual(len(HOOK_KINDS), 3)

            res = bootstrap_verify_harness(str(repo), force=True)
            self.assertTrue(res["preserved_hooks"])
            saved = {h["name"]: h for h in json.loads((repo / ".factonio" / "harness.json").read_text())["hooks"]}
            self.assertEqual((saved["model"]["covers"], saved["model"]["sync_commit"]), (["api/**"], "HEAD"))
            self.assertEqual((saved["pixels"]["baseline_paths"], saved["pixels"]["frozen_baseline"]), (["snapshots/**"], True))

            (repo / ".factonio" / "harness.json").unlink()
            parsed = {h["name"]: h for h in discover_harness(repo)["hooks"]}
            self.assertEqual((parsed["model"]["covers"], parsed["model"]["sync_commit"]), (["api/**"], "HEAD"))
            self.assertEqual((parsed["pixels"]["baseline_paths"], parsed["pixels"]["frozen_baseline"]), (["snapshots/**"], True))


class TestFormalInvariantDrift(unittest.TestCase):
    """Read-only `stale_invariants` drift vs unresolved `sync_commit` vs failing required hook."""

    def _repo(self, home: Path, hooks: list[dict[str, Any]]) -> tuple[Path, str]:
        from factonio.harness import bootstrap_verify_harness

        repo = _make_repo(home)
        initial = _git(repo, "rev-parse", "HEAD")
        bootstrap_verify_harness(str(repo), hooks=hooks)
        _commit_all(repo, "harness")
        return repo, initial

    def test_covered_change_after_sync_commit_is_stale_doc_drift_and_read_only(self):
        from factonio.harness import audit_harness, check_harness

        with isolated_hermes_home() as home:
            repo = _make_repo(home)
            sync = _git(repo, "rev-parse", "HEAD")
            from factonio.harness import bootstrap_verify_harness

            bootstrap_verify_harness(str(repo), hooks=[
                {"name": "api-model", "kind": "invariant", "command": "true", "covers": ["api/**"], "sync_commit": sync},
                {"name": "cli-model", "kind": "invariant", "command": "true", "covers": ["svc/**"], "optional": True},
            ])
            _commit_all(repo, "harness")
            chk = check_harness(str(repo), run_doctor=True, run_hooks=True)
            self.assertTrue(chk["valid"], chk["errors"])
            self.assertEqual(chk["warnings"], [])
            formal = chk["formal_invariants"]
            self.assertTrue(formal["checked"])
            self.assertEqual([(i["name"], i["status"]) for i in formal["invariants"]],
                             [("api-model", "in_sync"), ("cli-model", "in_sync")])
            self.assertEqual(formal["invariants"][1]["sync_source"], "harness.json base_commit")
            clean = audit_harness(str(repo))
            self.assertEqual((clean["severity"], clean["stale_invariants"]), ("clean", []))

            _write(repo, "api/server.py", "# changed\n")
            _commit_all(repo, "touch api")
            _write(repo, "svc/cli.py", "# changed\n")
            before_status = _git(repo, "status", "--porcelain")  # also refreshes the stat cache before fingerprinting
            before_index = _index_fingerprint(repo)
            audit = audit_harness(str(repo))
            self.assertEqual(_index_fingerprint(repo), before_index)
            self.assertEqual(_git(repo, "status", "--porcelain"), before_status)
            self.assertEqual((audit["drifted"], audit["severity"]), (True, "stale"))
            stale = {i["name"]: i for i in audit["stale_invariants"]}
            self.assertEqual(stale["api-model"], {
                "name": "api-model", "sync_commit": sync, "sync_source": "sync_commit", "covers": ["api/**"],
                "changed_files": ["api/server.py"], "optional": False,
            })
            self.assertEqual(stale["cli-model"]["changed_files"], ["svc/cli.py"])
            self.assertTrue(stale["cli-model"]["optional"])
            self.assertEqual((audit["unresolved_sync_commits"], audit["manifest_errors"]), ([], []))
            self.assertEqual(audit["drift_classification"]["classification"], "doc_drift")
            self.assertTrue(any("formal invariants" in r for r in audit["drift_classification"]["reasons"]))
            self.assertTrue(any("bump `sync_commit`" in r for r in audit["recommendations"]))
            chk = check_harness(str(repo))
            self.assertTrue(chk["valid"], chk["errors"])
            self.assertEqual({i["name"] for i in chk["formal_invariants"]["stale_invariants"]}, {"api-model", "cli-model"})

            run = TestSurfaces()._cli()
            code, out, _ = run("harness", "audit", str(repo), "--json")
            self.assertEqual(code, 1)
            self.assertEqual(len(json.loads(out)["stale_invariants"]), 2)
            code, out, _ = run("harness", "audit", str(repo))
            self.assertIn("stale_invariants: api-model (sync_commit", out)
            code, out, _ = run("harness", "check", str(repo))
            self.assertEqual(code, 0, out)
            self.assertIn("Invariant: [stale] api-model covers api/**", out)

    def test_unresolved_sync_commit_is_harness_gap_but_hooks_still_run(self):
        from factonio.harness import audit_harness, check_harness, classify_harness_drift

        with isolated_hermes_home() as home:
            repo, _ = self._repo(home, [
                {"name": "model", "kind": "invariant", "command": "true", "covers": ["api/**"], "sync_commit": "ab" * 20},
                {"name": "tests", "kind": "custom", "command": "true"},
            ])
            chk = check_harness(str(repo), run_doctor=True, run_hooks=True)
            self.assertFalse(chk["valid"])
            self.assertEqual(chk["formal_invariants"]["unresolved_sync_commits"], [{"name": "model", "sync_commit": "ab" * 20}])
            self.assertTrue(any("does not resolve" in e for e in chk["manifest_errors"]))
            self.assertEqual(chk["hook_results"]["ran"], 2)
            self.assertEqual(classify_harness_drift(None, chk)["classification"], "harness_gap")
            audit = audit_harness(str(repo))
            self.assertEqual((audit["drifted"], audit["severity"]), (True, "stale"))
            self.assertEqual(audit["drift_classification"]["classification"], "harness_gap")
            self.assertEqual(audit["stale_invariants"], [])

    def test_failing_required_formal_hook_outranks_as_product_regression(self):
        from factonio.harness import audit_harness, check_harness, classify_harness_drift

        with isolated_hermes_home() as home:
            repo, _ = self._repo(home, [
                {"name": "model", "kind": "invariant", "command": "exit 1", "covers": ["api/**"], "sync_commit": "no-such"},
            ])
            _write(repo, "api/server.py", "# drift\n")
            audit = audit_harness(str(repo))
            chk = check_harness(str(repo), run_doctor=True, run_hooks=True)
            self.assertEqual(chk["hook_results"]["failed_required"], ["model"])
            res = classify_harness_drift(audit, chk)
            self.assertEqual(res["classification"], "product_regression")
            self.assertEqual(res["classifications"], ["product_regression", "harness_gap", "doc_drift"])

    def test_dead_cover_glob_is_check_error_and_doc_drift(self):
        from factonio.harness import audit_harness, check_harness

        with isolated_hermes_home() as home:
            repo, _ = self._repo(home, [{"name": "model", "kind": "invariant", "command": "true", "covers": ["nope/**"]}])
            chk = check_harness(str(repo))
            self.assertFalse(chk["valid"])
            self.assertEqual(chk["formal_invariants"]["dead_cover_globs"], [{"hook": "model", "glob": "nope/**"}])
            self.assertTrue(any("covers glob `nope/**` matches no files" in e for e in chk["errors"]))
            self.assertEqual(chk["manifest_errors"], [])
            audit = audit_harness(str(repo))
            self.assertEqual(audit["dead_cover_globs"], [{"hook": "model", "glob": "nope/**"}])
            self.assertEqual(audit["drift_classification"]["classification"], "doc_drift")

    def test_every_git_probe_is_no_optional_locks(self):
        import factonio.harness as harness

        with isolated_hermes_home() as home:
            repo, sync = self._repo(home, [
                {"name": "model", "kind": "invariant", "command": "true", "covers": ["api/**"], "sync_commit": "HEAD~1"},
                {"name": "pixels", "kind": "visual_diff", "command": "true", "baseline_paths": ["api/**"]},
            ])
            _write(repo, "api/server.py", "# drift\n")
            calls: list[list[str]] = []
            real_run = subprocess.run

            def spy(cmd, *args, **kwargs):
                if isinstance(cmd, list) and cmd and cmd[0] == "git":
                    calls.append(list(cmd))
                return real_run(cmd, *args, **kwargs)

            with mock.patch.object(harness.subprocess, "run", side_effect=spy):
                audit = harness.audit_harness(str(repo))
                harness.check_harness(str(repo), base_ref=sync)
            self.assertTrue(audit["stale_invariants"] and audit["visual_baseline_changes"])
            self.assertTrue(any("diff" in c for c in calls))
            self.assertEqual([c for c in calls if c[1] != "--no-optional-locks"], [])


class TestFrozenVisualBaselines(unittest.TestCase):
    """`visual_diff` anti-shortcut: regenerated/loosened commands and tampered frozen baselines."""

    COMMAND = "npx playwright test --max-diff-pixels=0"

    def _repo(self, home: Path, **meta: Any) -> tuple[Path, str]:
        from factonio.harness import bootstrap_verify_harness

        repo = _make_repo(home)
        _write(repo, "snapshots/home.png", "png-v1\n")
        bootstrap_verify_harness(str(repo), hooks=[
            {"name": "lint", "kind": "invariant", "command": "true"},
            {"name": "pixels", "kind": "visual_diff", "command": self.COMMAND,
             "baseline_paths": ["snapshots/**"], **meta},
        ])
        return repo, _commit_all(repo, "harness + baselines")

    @staticmethod
    def _set_pixels(repo: Path, **changes: Any) -> None:
        hj = repo / ".factonio" / "harness.json"
        manifest = json.loads(hj.read_text(encoding="utf-8"))
        for hook in manifest["hooks"]:
            if hook["name"] == "pixels":
                for key, value in changes.items():
                    if value is None:
                        hook.pop(key, None)
                    else:
                        hook[key] = value
        hj.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    def test_regeneration_and_threshold_mutations_are_gate_loosening(self):
        from factonio.harness import audit_harness, check_harness, detect_harness_gate_loosening

        with isolated_hermes_home() as home:
            repo, _ = self._repo(home)
            clean = detect_harness_gate_loosening(repo)
            self.assertEqual((clean["loosened"], clean["loosened_visual_hooks"], clean["visual_baseline_changes"]),
                             (False, [], []))
            hj = repo / ".factonio" / "harness.json"
            committed = hj.read_text(encoding="utf-8")
            loosening = {
                "--update-snapshots": {"command": self.COMMAND + " --update-snapshots"},
                "-u": {"command": self.COMMAND + " -u"},
                "--update": {"command": self.COMMAND + " --update"},
                "--regenerate": {"command": self.COMMAND + " --regenerate"},
                "UPDATE_SNAPSHOTS=1": {"command": "UPDATE_SNAPSHOTS=1 " + self.COMMAND},
                "max-diff-pixels": {"command": "npx playwright test --max-diff-pixels=500"},
                "threshold": {"command": self.COMMAND + " --threshold 0.3"},
                "unfrozen": {"frozen_baseline": False},
                "unpinned": {"baseline_paths": None, "frozen_baseline": None},
            }
            for label, change in loosening.items():
                with self.subTest(label):
                    hj.write_text(committed, encoding="utf-8")
                    self._set_pixels(repo, **change)
                    before_index = _index_fingerprint(repo)
                    gate = detect_harness_gate_loosening(repo)
                    self.assertEqual(_index_fingerprint(repo), before_index)
                    self.assertTrue(gate["loosened"], gate)
                    self.assertEqual(gate["loosened_visual_hooks"], ["pixels"])
                    self.assertTrue(any("visual_diff hook 'pixels'" in f for f in gate["findings"]), gate["findings"])
                    chk = check_harness(str(repo))
                    self.assertFalse(chk["valid"])
                    audit = audit_harness(str(repo))
                    self.assertEqual(audit["gate_loosening"]["loosened_visual_hooks"], ["pixels"])
                    self.assertEqual(audit["drift_classification"]["classification"], "doc_drift")

            for label, change in {
                "tightened": {"command": "npx playwright test --max-diff-pixels=0 --threshold 0"},
                "reworded": {"command": "npx playwright test --reporter=line --max-diff-pixels=0"},
                "zero-env": {"command": "UPDATE_SNAPSHOTS=0 " + self.COMMAND},
            }.items():
                with self.subTest(label):
                    hj.write_text(committed, encoding="utf-8")
                    self._set_pixels(repo, **change)
                    gate = detect_harness_gate_loosening(repo)
                    self.assertFalse(gate["loosened"], gate["findings"])

    def test_tampered_frozen_baseline_is_flagged_but_new_or_unfrozen_is_not(self):
        from factonio.harness import audit_harness, check_harness, detect_harness_gate_loosening

        with isolated_hermes_home() as home:
            repo, harness_commit = self._repo(home)
            _write(repo, "snapshots/new-screen.png", "png-new\n")
            self.assertFalse(detect_harness_gate_loosening(repo)["loosened"])
            self.assertEqual(audit_harness(str(repo))["visual_baseline_changes"], [])

            _write(repo, "snapshots/home.png", "png-regenerated\n")
            before_index = _index_fingerprint(repo)
            gate = detect_harness_gate_loosening(repo)
            self.assertEqual(_index_fingerprint(repo), before_index)
            self.assertEqual(gate["visual_baseline_changes"], [
                {"name": "pixels", "baseline_paths": ["snapshots/**"], "changed_files": ["snapshots/home.png"]}
            ])
            self.assertTrue(gate["loosened"])
            chk = check_harness(str(repo))
            self.assertFalse(chk["valid"])
            self.assertEqual(chk["visual_baseline_changes"][0]["changed_files"], ["snapshots/home.png"])
            audit = audit_harness(str(repo))
            self.assertEqual(audit["visual_baseline_changes"][0]["changed_files"], ["snapshots/home.png"])
            self.assertEqual((audit["drifted"], audit["drift_classification"]["classification"]), (True, "doc_drift"))
            self.assertTrue(any("Restore the modified frozen visual baselines" in r for r in audit["recommendations"]))

            _commit_all(repo, "regenerate baselines")
            self.assertTrue(check_harness(str(repo))["valid"])
            committed = check_harness(str(repo), base_ref=harness_commit)
            self.assertFalse(committed["valid"])
            self.assertEqual(committed["visual_baseline_changes"][0]["changed_files"], ["snapshots/home.png"])
            self.assertEqual(audit_harness(str(repo))["visual_baseline_changes"][0]["name"], "pixels")

        with isolated_hermes_home() as home:
            repo, _ = self._repo(home, frozen_baseline=False)
            _write(repo, "snapshots/home.png", "png-regenerated\n")
            self.assertFalse(detect_harness_gate_loosening(repo)["loosened"])
            self.assertEqual(audit_harness(str(repo))["visual_baseline_changes"], [])

    def _assert_tampered(self, repo: Path, base_ref: Any = None, path: str = "snapshots/home.png") -> None:
        from factonio.harness import audit_harness, check_harness, detect_harness_gate_loosening

        before_index = _index_fingerprint(repo)
        gate = detect_harness_gate_loosening(repo, base_ref=base_ref) if base_ref else detect_harness_gate_loosening(repo)
        chk = check_harness(str(repo), base_ref=base_ref)
        audit = audit_harness(str(repo), base_ref=base_ref)
        self.assertEqual(_index_fingerprint(repo), before_index)
        self.assertTrue(gate["loosened"], gate)
        self.assertIn(path, gate["visual_baseline_changes"][0]["changed_files"])
        self.assertFalse(chk["valid"])
        self.assertIn(path, chk["visual_baseline_changes"][0]["changed_files"])
        self.assertTrue(audit["drifted"])
        self.assertEqual(audit["severity"], "stale")
        self.assertIn(path, audit["visual_baseline_changes"][0]["changed_files"])

    def test_staged_removal_or_rename_with_replacement_is_flagged(self):
        from factonio.harness import detect_harness_gate_loosening

        scenarios = {
            "rm-cached": [("git", "rm", "-q", "--cached", "snapshots/home.png")],
            "rm-cached+regenerated": [("git", "rm", "-q", "--cached", "snapshots/home.png"),
                                      ("write", "snapshots/home.png")],
            "rm+recreated": [("git", "rm", "-q", "snapshots/home.png"), ("write", "snapshots/home.png")],
            "mv+recreated": [("git", "mv", "snapshots/home.png", "snapshots/home_backup.png"),
                             ("write", "snapshots/home.png")],
            "rm-cached+recreated+add": [("git", "rm", "-q", "--cached", "snapshots/home.png"),
                                        ("write", "snapshots/home.png"), ("git", "add", "snapshots/home.png")],
        }
        for label, steps in scenarios.items():
            with self.subTest(label), isolated_hermes_home() as home:
                repo, _ = self._repo(home)
                for step in steps:
                    if step[0] == "write":
                        _write(repo, step[1], "png-regenerated\n")
                    else:
                        _git(repo, *step[1:])
                self._assert_tampered(repo)

        for replacement in ("untracked", "staged"):
            with self.subTest(f"committed-untrack+{replacement}"), isolated_hermes_home() as home:
                repo, harness_commit = self._repo(home)
                _git(repo, "rm", "-q", "--cached", "snapshots/home.png")
                _git(repo, "commit", "-q", "-m", "untrack baseline")
                _write(repo, "snapshots/home.png", "png-regenerated\n")
                if replacement == "staged":
                    _git(repo, "add", "snapshots/home.png")
                self._assert_tampered(repo, base_ref=harness_commit)

        with isolated_hermes_home() as home:
            repo, harness_commit = self._repo(home)
            _write(repo, "snapshots/brand_new.png", "png-new\n")
            self.assertFalse(detect_harness_gate_loosening(repo)["loosened"])
            _git(repo, "add", "snapshots/brand_new.png")
            self.assertFalse(detect_harness_gate_loosening(repo)["loosened"])
            _git(repo, "commit", "-q", "-m", "new baseline")
            self.assertFalse(detect_harness_gate_loosening(repo, base_ref=harness_commit)["loosened"])
            _write(repo, "snapshots/brand_new.png", "png-new-v2\n")
            self.assertEqual(
                detect_harness_gate_loosening(repo, base_ref=harness_commit)["visual_baseline_changes"], []
            )

    def test_disabled_regeneration_values_are_not_active_regeneration(self):
        from factonio.harness import detect_harness_gate_loosening

        hj_cases = {
            "=none -> =all": ("--update-snapshots=none", "--update-snapshots=all", True),
            "=none -> bare": ("--update-snapshots=none", "--update-snapshots", True),
            "none -> all (separate)": ("--update-snapshots none", "--update-snapshots all", True),
            "=false -> =all": ("--update-snapshots=false", "--update-snapshots=all", True),
            "plain -> =none": ("", "--update-snapshots=none", False),
            "plain -> =false": ("", "--update-snapshots=false", False),
            "plain -> none (separate)": ("", "--update-snapshots none", True),  # fail closed: only `=none` is inert
        }
        for label, (base_flag, live_flag, loosened) in hj_cases.items():
            with self.subTest(label), isolated_hermes_home() as home:
                base_cmd = (self.COMMAND + " " + base_flag).strip()
                repo, _ = self._repo(home, command=base_cmd)
                self._set_pixels(repo, command=self.COMMAND + " " + live_flag)
                gate = detect_harness_gate_loosening(repo)
                self.assertEqual(gate["loosened"], loosened, gate["findings"])
                self.assertEqual(gate["loosened_visual_hooks"], ["pixels"] if loosened else [])

    def test_repos_without_optional_fields_stay_clean(self):
        from factonio.harness import audit_harness, check_harness

        with isolated_hermes_home() as home:
            repo, _ = TestFormalInvariantDrift()._repo(home, [
                {"name": "lint", "kind": "invariant", "command": "true"},
                {"name": "pixels", "kind": "visual_diff", "command": "true -u"},
            ])
            _write(repo, "snapshots/home.png", "x\n")
            chk = check_harness(str(repo), run_doctor=True, run_hooks=True)
            self.assertTrue(chk["valid"], chk["errors"])
            self.assertEqual(chk["warnings"], [])
            self.assertFalse(chk["formal_invariants"]["checked"])
            self.assertEqual(chk["visual_baseline_changes"], [])
            self.assertFalse(chk["gate_loosening"]["loosened"])
            (repo / "snapshots" / "home.png").unlink()
            audit = audit_harness(str(repo))
            self.assertEqual((audit["severity"], audit["drifted"]), ("clean", False))
            for key in ("stale_invariants", "dead_cover_globs", "unresolved_sync_commits", "visual_baseline_changes"):
                self.assertEqual(audit[key], [], key)

    def test_agent_tool_schema_and_handler_surface_metadata(self):
        from factonio.agent_tools import register_tools

        with isolated_hermes_home() as home:
            ctx = _FakeCtx()
            register_tools(ctx)
            tool = ctx.tools["factonio_bootstrap_verify"]
            props = tool["schema"]["parameters"]["properties"]["hooks"]["items"]["properties"]
            for key in ("covers", "sync_commit", "baseline_paths", "frozen_baseline"):
                self.assertIn(key, props)
            repo = _make_repo(home)
            _write(repo, "snapshots/home.png", "png-v1\n")
            res = json.loads(tool["handler"]({"action": "init", "repo": str(repo), "hooks": [
                {"name": "pixels", "kind": "visual_diff", "command": "true", "baseline_paths": ["snapshots/**"]},
                {"name": "model", "kind": "invariant", "command": "true", "covers": ["api/**"]},
            ]}))
            self.assertTrue(res["written"], res)
            self.assertIs(res["manifest"]["hooks"][0]["frozen_baseline"], True)
            _commit_all(repo, "harness")
            _write(repo, "snapshots/home.png", "png-v2\n")
            chk = json.loads(tool["handler"]({"action": "check", "repo": str(repo)}))
            self.assertFalse(chk["valid"])
            self.assertEqual(chk["visual_baseline_changes"][0]["name"], "pixels")
            self.assertTrue(chk["formal_invariants"]["checked"])
            bad = json.loads(tool["handler"]({"action": "init", "repo": str(repo), "force": True, "hooks": [
                {"name": "x", "kind": "custom", "command": "true", "covers": ["api/**"]},
            ]}))
            self.assertFalse(bad.get("ok", True))


class TestFixRound2VisualGuardAndHarnessGaps(unittest.TestCase):
    """Post-hook frozen-baseline re-check, verify.md hook validation and the audit gate base."""

    @staticmethod
    def _visual_repo(home: Path, command: Any, *, name: str = "svc") -> Path:
        from factonio.harness import bootstrap_verify_harness

        repo = _make_repo(home, name)
        _write(repo, "snapshots/home.png", "png-v1\n")
        bootstrap_verify_harness(str(repo), hooks=[
            {"name": "pixels", "kind": "visual_diff", "command": command, "baseline_paths": ["snapshots/**"]},
        ])
        _commit_all(repo, "harness + baselines")
        return repo

    @staticmethod
    def _set_command(repo: Path, command: Any) -> None:
        hj = repo / ".factonio" / "harness.json"
        manifest = json.loads(hj.read_text(encoding="utf-8"))
        manifest["hooks"][0]["command"] = command
        hj.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    def test_check_run_hooks_rechecks_frozen_baselines_after_hooks(self):
        from factonio.harness import check_harness, classify_harness_drift

        with isolated_hermes_home() as home:
            repo = self._visual_repo(home, "printf png-regenerated > snapshots/home.png")
            before = check_harness(str(repo))
            self.assertTrue(before["valid"], before["errors"])
            chk = check_harness(str(repo), run_hooks=True)
            self.assertEqual((repo / "snapshots" / "home.png").read_text(encoding="utf-8"), "png-regenerated")
            self.assertEqual(chk["hook_results"]["failed_required"], [])
            self.assertFalse(chk["valid"])
            self.assertFalse(chk["ok"])
            self.assertTrue(chk["gate_loosening"]["loosened"])
            self.assertEqual(chk["visual_baseline_changes"], [
                {"name": "pixels", "baseline_paths": ["snapshots/**"], "changed_files": ["snapshots/home.png"]}
            ])
            self.assertEqual(chk["gate_loosening"]["visual_baseline_changes"], chk["visual_baseline_changes"])
            self.assertTrue(any("frozen baseline of visual_diff hook 'pixels'" in e for e in chk["errors"]))
            self.assertEqual(classify_harness_drift(None, chk)["classification"], "doc_drift")

        with isolated_hermes_home() as home:
            repo = self._visual_repo(home, "printf png-new > snapshots/brand_new.png")
            chk = check_harness(str(repo), run_hooks=True)
            self.assertTrue(chk["valid"], chk["errors"])
            self.assertEqual(chk["visual_baseline_changes"], [])

    def _assert_md_hooks_harness_gap(self, repo: Path) -> None:
        from factonio.harness import audit_harness, check_harness, classify_harness_drift

        for kwargs in ({}, {"run_doctor": True, "run_hooks": True}):
            chk = check_harness(str(repo), **kwargs)
            self.assertFalse(chk["valid"], kwargs)
            self.assertTrue(any(e.startswith("verify.md hooks invalid:") for e in chk["errors"]), chk["errors"])
            self.assertTrue(any(e.startswith("verify.md hooks invalid:") for e in chk["manifest_errors"]), chk)
            res = classify_harness_drift(None, chk)
            self.assertEqual(res["classification"], "harness_gap", res)
            self.assertTrue(res["reasons"][0].startswith("harness_gap: Malformed .factonio/verify.md"), res)
        audit = audit_harness(str(repo))
        self.assertEqual((audit["drifted"], audit["severity"]), (True, "stale"))
        self.assertTrue(any(e.startswith("verify.md hooks invalid:") for e in audit["manifest_errors"]), audit)
        self.assertEqual(audit["drift_classification"]["classification"], "harness_gap")

    def test_malformed_verify_md_hook_metadata_is_harness_gap(self):
        from factonio.harness import bootstrap_verify_harness

        md_line = "- **lint** (`custom`, required): `true`"
        with isolated_hermes_home() as home:
            repo = _make_repo(home)
            bootstrap_verify_harness(str(repo), hooks=[{"name": "lint", "kind": "custom", "command": "true"}])
            hj = repo / ".factonio" / "harness.json"
            manifest = json.loads(hj.read_text(encoding="utf-8"))
            del manifest["hooks"]
            hj.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            md = repo / ".factonio" / "verify.md"
            text = md.read_text(encoding="utf-8")
            self.assertIn(md_line, text)
            md.write_text(text.replace(md_line, md_line + " [covers: `api/**`]"), encoding="utf-8")
            _commit_all(repo, "hook-less harness.json + malformed verify.md hook")
            self._assert_md_hooks_harness_gap(repo)

        with isolated_hermes_home() as home:
            repo = _make_repo(home)
            bootstrap_verify_harness(str(repo), hooks=[{"name": "model", "kind": "invariant", "command": "true"}])
            (repo / ".factonio" / "harness.json").unlink()
            md = repo / ".factonio" / "verify.md"
            line = "- **model** (`invariant`, required): `true`"
            text = md.read_text(encoding="utf-8")
            self.assertIn(line, text)
            md.write_text(text.replace(line, line + " [sync_commit: `HEAD`]"), encoding="utf-8")
            _commit_all(repo, "verify.md-only harness with sync_commit but no covers")
            self._assert_md_hooks_harness_gap(repo)

    def test_default_audit_after_init_gates_against_last_harness_commit(self):
        from factonio.harness import audit_harness, bootstrap_verify_harness

        with isolated_hermes_home() as home:
            repo = _make_repo(home)
            _write(repo, "snapshots/home.png", "png-v1\n")
            c1 = _commit_all(repo, "baseline")
            bootstrap_verify_harness(str(repo), hooks=[
                {"name": "pixels", "kind": "visual_diff", "command": "true", "baseline_paths": ["snapshots/**"]},
            ])
            self.assertEqual(json.loads((repo / ".factonio" / "harness.json").read_text())["base_commit"], c1)
            c2 = _commit_all(repo, "harness")
            clean = audit_harness(str(repo))
            self.assertEqual((clean["severity"], clean["gate_loosening"]["checked"]), ("clean", True))
            self.assertEqual(clean["gate_loosening"]["base_commit"], c2)
            _write(repo, "snapshots/home.png", "png-regenerated\n")
            _commit_all(repo, "regenerate baseline")
            audit = audit_harness(str(repo))
            self.assertEqual(audit["base_commit"], c1)
            gate = audit["gate_loosening"]
            self.assertEqual((gate["checked"], gate["loosened"], gate["base_commit"]), (True, True, c2))
            self.assertEqual(gate["visual_baseline_changes"], audit["visual_baseline_changes"])
            self.assertEqual(audit["visual_baseline_changes"][0]["changed_files"], ["snapshots/home.png"])
            self.assertTrue(any("frozen baseline" in f for f in gate["findings"]))

            run = TestSurfaces()._cli()
            code, out, _ = run("harness", "audit", str(repo), "--json")
            self.assertEqual(code, 1)
            self.assertTrue(json.loads(out)["gate_loosening"]["loosened"])


class TestFixRound3PinnedAuditBase(unittest.TestCase):
    """Default audit keeps a pinned approved `base_commit`; non-finite thresholds; missing-harness baselines."""

    @staticmethod
    def _pinned_repo(home: Path) -> tuple[Path, str]:
        from factonio.harness import bootstrap_verify_harness

        repo = _make_repo(home)
        _write(repo, "snapshots/home.png", "png-v1\n")
        _commit_all(repo, "baseline")
        bootstrap_verify_harness(str(repo), hooks=[
            {"name": "req-inv", "kind": "invariant", "command": "true"},
            {"name": "ui-snap", "kind": "visual_diff", "command": "true", "baseline_paths": ["snapshots/**"],
             "frozen_baseline": True},
        ])
        approved = _commit_all(repo, "approved harness")
        hj = repo / ".factonio" / "harness.json"
        manifest = json.loads(hj.read_text(encoding="utf-8"))
        manifest["base_commit"] = approved
        hj.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        _commit_all(repo, "pin base_commit")
        return repo, approved

    @staticmethod
    def _edit(repo: Path, mutate: Any) -> None:
        hj = repo / ".factonio" / "harness.json"
        manifest = json.loads(hj.read_text(encoding="utf-8"))
        mutate(manifest)
        hj.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    def test_default_audit_gates_against_pinned_base_commit(self):
        from factonio.harness import audit_harness

        def drop_hook(m):
            m["hooks"] = [h for h in m["hooks"] if h["name"] != "req-inv"]

        def downgrade_and_drop_doctor(m):
            for h in m["hooks"]:
                if h["name"] == "req-inv":
                    h["optional"] = True
            m["phases"]["doctor"] = m["phases"]["doctor"][1:]

        def regenerate(m):
            for h in m["hooks"]:
                if h["name"] == "ui-snap":
                    h["command"] = "true --update-snapshots"

        def unfreeze(m):
            for h in m["hooks"]:
                if h["name"] == "ui-snap":
                    h["frozen_baseline"] = False

        cases = {
            "removed": (drop_hook, "removed_required_hooks"),
            "downgraded": (downgrade_and_drop_doctor, "downgraded_hooks"),
            "regenerate": (regenerate, "loosened_visual_hooks"),
            "unfreeze": (unfreeze, "visual_baseline_changes"),
        }
        for label, (mutate, field) in cases.items():
            with self.subTest(label), isolated_hermes_home() as home:
                repo, approved = self._pinned_repo(home)
                clean = audit_harness(str(repo))
                self.assertEqual((clean["severity"], clean["gate_loosening"]["base_commit"]), ("clean", approved))
                self._edit(repo, mutate)
                if label == "unfreeze":
                    _write(repo, "snapshots/home.png", "png-regenerated\n")
                _commit_all(repo, f"loosen: {label}")
                audit = audit_harness(str(repo))
                gate = audit["gate_loosening"]
                self.assertEqual((audit["base_commit"], audit["base_source"]), (approved, "harness.json base_commit"))
                self.assertEqual((gate["checked"], gate["loosened"], gate["base_commit"]), (True, True, approved))
                self.assertTrue(gate[field], gate)
                self.assertTrue(gate["findings"])
                self.assertEqual((audit["drifted"], audit["severity"]), (True, "stale"))
                if label == "downgraded":
                    self.assertTrue(gate["removed_doctor_commands"], gate)
                if label == "unfreeze":
                    self.assertEqual(audit["visual_baseline_changes"][0]["changed_files"], ["snapshots/home.png"])
                code, out, _ = TestSurfaces()._cli()("harness", "audit", str(repo), "--json")
                self.assertEqual(code, 1)
                self.assertTrue(json.loads(out)["gate_loosening"]["loosened"])

    def test_non_finite_threshold_is_threshold_removal(self):
        from factonio.harness import detect_harness_gate_loosening

        for value in ("nan", "inf", "-inf", "NaN%"):
            with self.subTest(value), isolated_hermes_home() as home:
                repo = TestFixRound2VisualGuardAndHarnessGaps._visual_repo(home, "true --threshold 0.01")
                TestFixRound2VisualGuardAndHarnessGaps._set_command(repo, f"true --threshold {value}")
                gate = detect_harness_gate_loosening(repo)
                self.assertTrue(gate["loosened"], gate)
                self.assertEqual(gate["loosened_visual_hooks"], ["pixels"])
                self.assertTrue(any("threshold" in f for f in gate["findings"]), gate["findings"])

    def test_missing_harness_reports_top_level_baseline_changes(self):
        from factonio.harness import audit_harness

        with isolated_hermes_home() as home:
            repo = TestFixRound2VisualGuardAndHarnessGaps._visual_repo(home, "true")
            (repo / ".factonio" / "verify.md").unlink()
            _write(repo, "snapshots/home.png", "png-regenerated\n")
            audit = audit_harness(str(repo))
            self.assertEqual(audit["severity"], "missing")
            changes = audit["gate_loosening"]["visual_baseline_changes"]
            self.assertTrue(changes)
            self.assertEqual(audit["visual_baseline_changes"], changes)
            self.assertIn("snapshots/home.png", audit["visual_baseline_changes"][0]["changed_files"])


class TestHarnessDirFormalInvariants(unittest.TestCase):
    """Formal invariants covering `.factonio/` files are inventoried without leaking into the general inventory."""

    def test_invariant_covering_harness_dir_file_is_inventoried_and_drifts(self):
        from factonio.harness import audit_harness, bootstrap_verify_harness, check_harness

        with isolated_hermes_home() as home:
            repo = _make_repo(home)
            _write(repo, ".factonio/model.cfg", "states = 3\n")
            _commit_all(repo, "model")
            sync = _git(repo, "rev-parse", "HEAD")
            bootstrap_verify_harness(str(repo), hooks=[
                {"name": "model", "kind": "invariant", "command": "true", "covers": [".factonio/model.cfg"],
                 "sync_commit": sync},
            ])
            _commit_all(repo, "harness")
            chk = check_harness(str(repo), run_hooks=True)
            self.assertTrue(chk["valid"], chk["errors"])
            self.assertEqual(chk["formal_invariants"]["dead_cover_globs"], [])
            self.assertEqual([i["status"] for i in chk["formal_invariants"]["invariants"]], ["in_sync"])
            clean = audit_harness(str(repo))
            self.assertEqual((clean["severity"], clean["dead_cover_globs"], clean["stale_invariants"]),
                             ("clean", [], []))

            _write(repo, ".factonio/model.cfg", "states = 4\n")
            _commit_all(repo, "change model")
            before_index = _index_fingerprint(repo)
            chk = check_harness(str(repo))
            self.assertTrue(chk["valid"], chk["errors"])
            self.assertEqual([i["status"] for i in chk["formal_invariants"]["invariants"]], ["stale"])
            self.assertEqual([i["changed_files"] for i in chk["formal_invariants"]["stale_invariants"]],
                             [[".factonio/model.cfg"]])
            audit = audit_harness(str(repo))
            self.assertEqual(_index_fingerprint(repo), before_index)
            self.assertEqual((audit["drifted"], audit["severity"]), (True, "stale"))
            self.assertEqual([i["changed_files"] for i in audit["stale_invariants"]], [[".factonio/model.cfg"]])
            self.assertEqual(audit["drift_classification"]["classification"], "doc_drift")
            self.assertEqual(audit["orphaned_globs"], [])

    def test_harness_dir_stays_out_of_general_inventory(self):
        from factonio.harness import _changed_files, _list_repo_files

        with isolated_hermes_home() as home:
            repo = _make_repo(home)
            base = _git(repo, "rev-parse", "HEAD")
            _write(repo, ".factonio/model.cfg", "x\n")
            _write(repo, ".venv/lib/x.py", "x\n")
            self.assertNotIn(".factonio/model.cfg", _list_repo_files(repo)[0])
            self.assertNotIn(".factonio/model.cfg", _changed_files(repo, base))
            self.assertIn(".factonio/model.cfg", _changed_files(repo, base, include_harness_dir=True))


class TestFixRound9LiveFrozenBaselines(unittest.TestCase):
    """Live-declared frozen `visual_diff` baselines protect existing committed images."""

    _visual_repo = staticmethod(TestFixRound2VisualGuardAndHarnessGaps._visual_repo)

    @staticmethod
    def _repo_without_visual_hook(home: Path) -> Path:
        from factonio.harness import bootstrap_verify_harness

        repo = _make_repo(home)
        _write(repo, "snapshots/home.png", "png-v1\n")
        bootstrap_verify_harness(str(repo), hooks=[{"name": "lint", "kind": "invariant", "command": "true"}])
        _commit_all(repo, "harness + committed baseline image")
        return repo

    @staticmethod
    def _add_live_visual_hook(repo: Path, command: str, **meta: Any) -> None:
        hj = repo / ".factonio" / "harness.json"
        manifest = json.loads(hj.read_text(encoding="utf-8"))
        manifest["hooks"].append({"name": "pixels", "kind": "visual_diff", "command": command,
                                  "baseline_paths": ["snapshots/**"], **meta})
        hj.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    def test_live_added_frozen_hook_protects_existing_committed_baselines(self):
        from factonio.harness import audit_harness, check_harness, detect_harness_gate_loosening

        for meta in ({}, {"frozen_baseline": True}):
            with self.subTest(meta=meta), isolated_hermes_home() as home:
                repo = self._repo_without_visual_hook(home)
                self._add_live_visual_hook(repo, "true", **meta)
                clean = check_harness(str(repo), run_hooks=True)
                self.assertTrue(clean["valid"], clean["errors"])
                self.assertEqual(clean["visual_baseline_changes"], [])

                _write(repo, "snapshots/home.png", "png-tampered\n")
                gate = detect_harness_gate_loosening(repo)
                self.assertEqual(gate["visual_baseline_changes"], [
                    {"name": "pixels", "baseline_paths": ["snapshots/**"], "changed_files": ["snapshots/home.png"]}
                ])
                chk = check_harness(str(repo), run_hooks=True)
                self.assertFalse(chk["valid"])
                self.assertEqual(chk["visual_baseline_changes"][0]["changed_files"], ["snapshots/home.png"])
                self.assertEqual(audit_harness(str(repo))["visual_baseline_changes"][0]["changed_files"],
                                 ["snapshots/home.png"])
                _git(repo, "checkout", "--", "snapshots/home.png")

                # a hook that regenerates the existing image while it runs is caught by the post-hook re-check
                hj = repo / ".factonio" / "harness.json"
                manifest = json.loads(hj.read_text(encoding="utf-8"))
                manifest["hooks"][-1]["command"] = "printf regen > snapshots/home.png"
                hj.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
                self.assertFalse(detect_harness_gate_loosening(repo)["loosened"])
                post = check_harness(str(repo), run_hooks=True)
                self.assertEqual(post["hook_results"]["failed_required"], [])
                self.assertFalse(post["valid"])
                self.assertEqual(post["visual_baseline_changes"][0]["changed_files"], ["snapshots/home.png"])

    def test_live_added_frozen_hook_allows_brand_new_baselines_and_unfrozen_hooks(self):
        from factonio.harness import check_harness, detect_harness_gate_loosening

        with isolated_hermes_home() as home:
            repo = self._repo_without_visual_hook(home)
            self._add_live_visual_hook(repo, "printf new > snapshots/new-screen.png")
            _write(repo, "snapshots/other.png", "png-new\n")
            chk = check_harness(str(repo), run_hooks=True)
            self.assertTrue((repo / "snapshots" / "new-screen.png").exists())
            self.assertTrue(chk["valid"], chk["errors"])
            self.assertEqual(chk["visual_baseline_changes"], [])
        with isolated_hermes_home() as home:
            repo = self._repo_without_visual_hook(home)
            self._add_live_visual_hook(repo, "true", frozen_baseline=False)
            _write(repo, "snapshots/home.png", "png-regenerated\n")
            self.assertFalse(detect_harness_gate_loosening(repo)["loosened"])
            self.assertTrue(check_harness(str(repo), run_hooks=True)["valid"])

    def test_live_hook_cannot_unfreeze_or_drop_committed_frozen_globs(self):
        from factonio.harness import check_harness

        with isolated_hermes_home() as home:
            repo = self._visual_repo(home, "true")
            _write(repo, "snapshots/home.png", "png-tampered\n")
            for meta in ({"frozen_baseline": False}, {"baseline_paths": ["other/**"]}):
                with self.subTest(meta=meta):
                    hj = repo / ".factonio" / "harness.json"
                    manifest = json.loads(hj.read_text(encoding="utf-8"))
                    manifest["hooks"][0].update({"baseline_paths": ["snapshots/**"], "frozen_baseline": True, **meta})
                    hj.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
                    chk = check_harness(str(repo))
                    self.assertFalse(chk["valid"])
                    self.assertEqual(chk["visual_baseline_changes"][0]["changed_files"], ["snapshots/home.png"])


class TestFixRound10FirstHarnessFrozenBaselines(unittest.TestCase):
    """Frozen baselines of a first, not-yet-committed harness (`baseline_source: live`)."""

    @staticmethod
    def _uncommitted_harness(home: Path, command: str, **meta: Any) -> tuple[Path, str]:
        """A repo whose committed `snapshots/home.png` predates a freshly initialised, uncommitted harness."""
        from factonio.harness import bootstrap_verify_harness

        repo = _make_repo(home)
        _write(repo, "snapshots/home.png", "png-v1\n")
        main_sha = _commit_all(repo, "committed baseline image, no harness yet")
        bootstrap_verify_harness(str(repo), hooks=[
            {"name": "pixels", "kind": "visual_diff", "command": command, "baseline_paths": ["snapshots/**"], **meta},
        ])
        return repo, main_sha

    def test_uncommitted_first_harness_protects_existing_committed_baselines(self):
        from factonio.harness import audit_harness, check_harness, detect_harness_gate_loosening

        expected = [{"name": "pixels", "baseline_paths": ["snapshots/**"], "changed_files": ["snapshots/home.png"]}]
        with isolated_hermes_home() as home:
            repo, _ = self._uncommitted_harness(home, "true")
            self.assertTrue(check_harness(str(repo), run_hooks=True)["valid"])
            _write(repo, "snapshots/home.png", "png-tampered\n")
            gate = detect_harness_gate_loosening(repo)
            self.assertEqual((gate["checked"], gate["loosened"], gate["baseline_source"]), (True, True, "live"))
            self.assertEqual(gate["visual_baseline_changes"], expected)
            chk = check_harness(str(repo))
            self.assertFalse(chk["valid"])
            self.assertEqual(chk["visual_baseline_changes"], expected)
            self.assertEqual(audit_harness(str(repo))["visual_baseline_changes"], expected)
        with isolated_hermes_home() as home:
            repo, _ = self._uncommitted_harness(home, "printf regen > snapshots/home.png")
            self.assertFalse(detect_harness_gate_loosening(repo)["loosened"])
            chk = check_harness(str(repo), run_hooks=True)
            self.assertEqual((repo / "snapshots" / "home.png").read_text(encoding="utf-8"), "regen")
            self.assertEqual(chk["hook_results"]["failed_required"], [])
            self.assertFalse(chk["valid"])
            self.assertTrue(chk["gate_loosening"]["loosened"], chk["gate_loosening"])
            self.assertEqual(chk["visual_baseline_changes"], expected)

    def test_pr_introducing_the_first_harness_cannot_modify_existing_baselines(self):
        from factonio.harness import audit_harness, check_harness

        with isolated_hermes_home() as home:
            repo, main_sha = self._uncommitted_harness(home, "true")
            _git(repo, "checkout", "-q", "-b", "feature")
            _write(repo, "snapshots/home.png", "png-regenerated\n")
            _commit_all(repo, "add harness and regenerate the committed baseline")
            chk = check_harness(str(repo), base_ref=main_sha)
            self.assertFalse(chk["valid"])
            self.assertEqual(chk["visual_baseline_changes"][0]["changed_files"], ["snapshots/home.png"])
            self.assertTrue(any("frozen baseline" in e for e in chk["errors"]), chk["errors"])
            audit = audit_harness(str(repo), base_ref=main_sha)
            self.assertEqual((audit["severity"], audit["gate_loosening"]["loosened"]), ("stale", True))
            self.assertTrue(check_harness(str(repo))["valid"])  # vs HEAD the committed image is the baseline

    def test_first_harness_controls_stay_clean(self):
        from factonio.harness import check_harness, detect_harness_gate_loosening

        with isolated_hermes_home() as home:  # brand-new baseline files are new surfaces, not tampering
            repo, main_sha = self._uncommitted_harness(home, "printf new > snapshots/new-screen.png")
            _write(repo, "snapshots/other.png", "png-new\n")
            chk = check_harness(str(repo), run_hooks=True)
            self.assertTrue((repo / "snapshots" / "new-screen.png").exists())
            self.assertTrue(chk["valid"], chk["errors"])
            self.assertEqual(chk["visual_baseline_changes"], [])
            _commit_all(repo, "harness + new baselines")
            self.assertTrue(check_harness(str(repo), base_ref=main_sha)["valid"])
        for meta in ({"frozen_baseline": False}, {"baseline_paths": None}):
            with self.subTest(meta=meta), isolated_hermes_home() as home:  # no frozen hook: Wave 4 contract
                repo, _ = self._uncommitted_harness(home, "printf regen > snapshots/home.png")
                hj = repo / ".factonio" / "harness.json"
                manifest = json.loads(hj.read_text(encoding="utf-8"))
                manifest["hooks"][0].update(meta)
                manifest["hooks"][0] = {k: v for k, v in manifest["hooks"][0].items() if v is not None}
                hj.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
                gate = detect_harness_gate_loosening(repo)
                self.assertEqual((gate["checked"], gate["loosened"], gate["baseline_source"]), (False, False, None))
                self.assertIn("no .factonio/ harness committed", gate["reason"])
                self.assertTrue(check_harness(str(repo), run_hooks=True)["valid"])

    def test_audit_still_falls_back_to_the_committed_harness(self):
        from factonio.harness import audit_harness

        with isolated_hermes_home() as home:
            repo, main_sha = self._uncommitted_harness(home, "true")
            hj = repo / ".factonio" / "harness.json"
            manifest = json.loads(hj.read_text(encoding="utf-8"))
            manifest["hooks"].append({"name": "lint", "kind": "invariant", "command": "true"})
            hj.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            _commit_all(repo, "commit harness")
            manifest["hooks"] = [h for h in manifest["hooks"] if h["name"] != "lint"]
            hj.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            _write(repo, "snapshots/home.png", "png-tampered\n")
            audit = audit_harness(str(repo), base_ref=main_sha)
            gate = audit["gate_loosening"]
            self.assertEqual(gate["removed_required_hooks"], ["lint"], gate)
            self.assertEqual(gate["visual_baseline_changes"][0]["changed_files"], ["snapshots/home.png"])
            self.assertEqual(audit["severity"], "stale")


class TestFixRound11ImmutablePreExecutionFrozenContract(unittest.TestCase):
    """The frozen-baseline contract fixed before Doctor/hooks run, even when they unfreeze or remove the hook."""

    _uncommitted_harness = staticmethod(TestFixRound10FirstHarnessFrozenBaselines._uncommitted_harness)

    # run by a hook or Doctor command: drop the `pixels` hook's frozen contract, then regenerate its baseline
    _MUTATE = ("import json, pathlib, sys\n"
               "hj = pathlib.Path('.factonio/harness.json')\n"
               "m = json.loads(hj.read_text())\n"
               "if sys.argv[1] == 'remove':\n"
               "    m['hooks'] = [h for h in m['hooks'] if h['name'] != 'pixels']\n"
               "else:\n"
               "    for h in m['hooks']:\n"
               "        if h['name'] == 'pixels':\n"
               "            h['frozen_baseline'] = False\n"
               "hj.write_text(json.dumps(m, indent=2))\n"
               "pathlib.Path('snapshots/home.png').write_text('regenerated')\n")
    _EXPECTED = [{"name": "pixels", "baseline_paths": ["snapshots/**"], "changed_files": ["snapshots/home.png"]}]

    def _assert_contract_kept(self, repo: Path, **run: bool) -> None:
        from factonio.harness import check_harness

        chk = check_harness(str(repo), **run)
        self.assertEqual((repo / "snapshots" / "home.png").read_text(encoding="utf-8"), "regenerated")
        self.assertFalse(chk["ok"])
        self.assertFalse(chk["valid"])
        self.assertTrue(chk["gate_loosening"]["loosened"], chk["gate_loosening"])
        self.assertEqual(chk["visual_baseline_changes"], self._EXPECTED)
        self.assertTrue(any("removed or unfrozen while Doctor/hooks ran" in e for e in chk["errors"]), chk["errors"])

    def test_first_harness_hook_cannot_unfreeze_itself_and_regenerate(self):
        for mode in ("unfreeze", "remove"):
            with self.subTest(mode=mode), isolated_hermes_home() as home:
                repo, _ = self._uncommitted_harness(home, f"python3 mutate.py {mode}")
                _write(repo, "mutate.py", self._MUTATE)
                self._assert_contract_kept(repo, run_hooks=True)

    def test_live_added_hook_cannot_unfreeze_itself_and_regenerate(self):
        from factonio.harness import bootstrap_verify_harness

        for mode in ("unfreeze", "remove"):
            with self.subTest(mode=mode), isolated_hermes_home() as home:
                repo = _make_repo(home)
                _write(repo, "snapshots/home.png", "png-v1\n")
                _write(repo, "mutate.py", self._MUTATE)
                bootstrap_verify_harness(str(repo), hooks=[{"name": "lint", "kind": "invariant", "command": "true"}])
                _commit_all(repo, "committed harness without a visual hook")
                hj = repo / ".factonio" / "harness.json"
                manifest = json.loads(hj.read_text(encoding="utf-8"))
                manifest["hooks"].append({"name": "pixels", "kind": "visual_diff", "baseline_paths": ["snapshots/**"],
                                          "command": f"python3 mutate.py {mode}"})
                hj.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
                self._assert_contract_kept(repo, run_hooks=True)

    def test_doctor_command_cannot_unfreeze_and_regenerate(self):
        for mode in ("unfreeze", "remove"):
            for committed in (False, True):
                with self.subTest(mode=mode, committed=committed), isolated_hermes_home() as home:
                    repo, _ = self._uncommitted_harness(home, "true")
                    _write(repo, "mutate.py", self._MUTATE)
                    hj = repo / ".factonio" / "harness.json"
                    manifest = json.loads(hj.read_text(encoding="utf-8"))
                    manifest.setdefault("phases", {})["doctor"] = [f"python3 mutate.py {mode}"]
                    hj.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
                    if committed:
                        _commit_all(repo, "harness with a mutating Doctor command")
                    self._assert_contract_kept(repo, run_doctor=True)

    def test_frozen_contract_controls_stay_clean(self):
        from factonio.harness import check_harness

        with isolated_hermes_home() as home:  # a new baseline file is a new surface, not tampering
            repo, _ = self._uncommitted_harness(home, "printf new > snapshots/new.png")
            chk = check_harness(str(repo), run_doctor=True, run_hooks=True)
            self.assertTrue(chk["valid"], chk["errors"])
            self.assertEqual(chk["visual_baseline_changes"], [])
        with isolated_hermes_home() as home:  # unfrozen before anything ran: the Wave 4 contract
            repo, _ = self._uncommitted_harness(home, "python3 mutate.py unfreeze", frozen_baseline=False)
            _write(repo, "mutate.py", self._MUTATE)
            chk = check_harness(str(repo), run_hooks=True)
            self.assertTrue(chk["valid"], chk["errors"])
            self.assertFalse(chk["gate_loosening"]["loosened"], chk["gate_loosening"])


class TestFailClosedStructuralVisualHookPolicy(unittest.TestCase):
    """The fail-closed structural `visual_diff` command guard: an unchanged committed command is always clean;
    an edited one passes only when it keeps the committed shell structure token for token and adds nothing but
    in-place threshold tightening or provably inert checker options. Shell rewrites fail closed."""

    _visual_repo = staticmethod(TestFixRound2VisualGuardAndHarnessGaps._visual_repo)
    _set_command = staticmethod(TestFixRound2VisualGuardAndHarnessGaps._set_command)
    # The visual diff is 0.25: the committed THRESHOLD=0.01 fails it, the lenient default (THRESHOLD unset) or a
    # regenerating `-u` passes it, so every bypass below turns the failing committed check green.
    _CHECK = ("import os, sys\n"
              "t = os.environ.get('THRESHOLD')\n"
              "sys.exit(0 if '-u' in sys.argv[1:] or 0.25 <= float(t if t is not None else '1.0') else 1)\n")
    _EXPORTED = "export THRESHOLD=0.01; python3 check.py"
    _UNVERIFIABLE = "unverifiable shell/structural rewrite"

    # Round 12 attack classes against the committed `_EXPORTED` hook, each executed by `/bin/sh` (dash or bash).
    _EXECUTED_BYPASSES = (
        "export threshold=0.01; python3 check.py",                                  # case-renamed variable
        "false && export THRESHOLD=0.01; python3 check.py",                         # short-circuited export
        "export THRESHOLD=0.01; bash -c 'exec -c python3 check.py'",               # bash `exec -c`
        "export THRESHOLD=0.01; bash -c 'exec -ca visual python3 check.py'",
        "export THRESHOLD=0.01; bash -c 'unset \"$0\"; python3 check.py' THRESHOLD",  # positional expansion
        "unset THRESHOLD; (export THRESHOLD=0.01); python3 check.py",               # subshell re-export
        "sh -c 'export THRESHOLD=0.01'; python3 check.py",                          # child-shell re-export
        "export THRESHOLD=0.01 | true; python3 check.py",                           # pipeline re-export
        "eval 'export THRESHOLD=0.01' | true; python3 check.py",                    # `eval` in a pipeline
        "{ export THRESHOLD=0.01; } | true; python3 check.py",                      # brace group in a pipeline
        "export THRESHOLD=0.01; env -u THRESHOLD python3 check.py",                 # env mutators
        "export THRESHOLD=0.01; env -i PATH=\"$PATH\" python3 check.py",
        "bash -c 'export -f THRESHOLD=0.01; python3 check.py'",                      # function-mode export
        "f() { export THRESHOLD=0.01; }; python3 check.py",                         # uncalled function body
        "export THRESHOLD=0.01; python3 check.py || true",                          # `||` fallback
        "export THRESHOLD=0.01; python3 check.py -u",                               # regeneration flag
        "export THRESHOLD=0.01; eval 'python3 check.py -u'",
        "export THRESHOLD=0.01\nunset THRESHOLD\npython3 check.py",                 # newline injection
    )
    # Parser-level attacks (bash/GNU-only forms or non-executing fixtures) that must be rejected as well.
    _REJECTED = {
        "env --split-str": (_EXPORTED, "export THRESHOLD=0.01; env --split-str='-u THRESHOLD python3 check.py'"),
        "env -S": (_EXPORTED, "export THRESHOLD=0.01; env -S '-u THRESHOLD python3 check.py'"),
        "env -": (_EXPORTED, "export THRESHOLD=0.01; env - python3 check.py"),
        "env -u $0": (_EXPORTED, "export THRESHOLD=0.01; bash -c 'env -u \"$0\" python3 check.py' THRESHOLD"),
        "export -f": (_EXPORTED, "export -f THRESHOLD=0.01; python3 check.py"),
        "assign + export -f": (_EXPORTED, "THRESHOLD=0.01; export -f THRESHOLD; python3 check.py"),
        "declare -xf": (_EXPORTED, "declare -xf THRESHOLD=0.01; python3 check.py"),
        "typeset -xf": (_EXPORTED, "typeset -xf THRESHOLD=0.01; python3 check.py"),
        "builtin eval": ("python3 check.py", "builtin eval 'python3 check.py -u'"),
        "python3 -c": ("python3 check.py", "python3 -c 'import sys; sys.exit(0)' check.py"),
        "option swallowing": ("npx playwright test --threshold 0.01", "npx playwright test --output --threshold 0.01"),
        "-- bypass": ("npx playwright test --threshold 0.01", "npx playwright test -- --threshold 0.01"),
        "new positive threshold": ("python3 check.py", "python3 check.py --threshold 0.5"),
        "threshold removed": ("python3 check.py --threshold 0.01", "python3 check.py"),
        "NaN": ("python3 check.py --threshold 0.01", "python3 check.py --threshold NaN"),
        "inf": ("python3 check.py --threshold 0.01", "python3 check.py --threshold=inf"),
        "% loosened": ("python3 check.py --threshold 1%", "python3 check.py --threshold 0.05"),
        "ratio to %": ("python3 check.py --threshold 0.05", "python3 check.py --threshold 10%"),
        "% to ratio": ("python3 check.py --threshold 5%", "python3 check.py --threshold 0.01"),
        "% vs pixels": ("python3 check.py --threshold 1%", "python3 check.py --threshold 5"),
        "swapped thresholds": ("a --threshold 0.01 && b --threshold 0.05", "a --threshold 0.05 && b --threshold 0.01"),
        "prefix loosened": ("THRESHOLD=0.05 python3 check.py", "THRESHOLD=0.5 python3 check.py"),
        "new env threshold": ("python3 check.py", "THRESHOLD=0 python3 check.py"),
        "jest -u": ("npx jest", "npx jest -u"),
        "bundled -vu": ("npx jest", "npx jest -vu"),
        "UPDATE_SNAPSHOTS=1": ("npx jest", "UPDATE_SNAPSHOTS=1 npx jest"),
        "--update-snapshots=all": ("npx playwright test --update-snapshots=none",
                                   "npx playwright test --update-snapshots=all"),
        "go -update": ("go test ./...", "go test ./... -update"),
        "argv -u": ("python3 check.py", ["python3", "check.py", "-u"]),
        "test filter": ("pytest tests/visual", "pytest tests/visual -k home"),
        "--grep=": ("npx playwright test", "npx playwright test --grep=home"),
        "--help exits 0": ("python3 check.py", "python3 check.py --help"),
        "positional added": ("npx playwright test", "npx playwright test home.spec.ts"),
        "sudo": ("python3 check.py", "sudo python3 check.py"),
        "--env-file=": ("uv run python3 check.py", "uv run --env-file=.env python3 check.py"),
        "--snapshot-dir=": ("npx playwright test", "npx playwright test --snapshot-dir=tmp"),
        "--project= filter": ("npx playwright test", "npx playwright test --project=chromium"),
        "-u not after the interpreter": ("npx jest python3 tests", "npx jest python3 -u tests"),
        "fd becomes positional": ("npx playwright test 2>err.log", "npx playwright test 2 >err.log"),
        "quoted expansion": ("python3 check.py '$HOME'", "python3 check.py $HOME"),
        "redirect target": ("npx playwright test > log.txt", "npx playwright test > UPDATE_SNAPSHOTS=0 log.txt"),
        "heredoc body": ("python3 - <<'EOF'\nt = 0.01\nEOF", "python3 - <<'EOF'\nt = 0.01 --True\nEOF"),
        "unclosed quote": ("python3 check.py", "python3 check.py 'unclosed"),
        "trailing backslash": ("python3 check.py", "python3 check.py \\"),
    }
    _UNCHANGED = (
        _EXPORTED, "env - python3 -u check.py", "env -S '-u FOO python3 -u check.py'",
        "bash -c 'export THRESHOLD=0.01; python3 check.py'", "go test -run TestVisual -fuzz=FuzzUI ./...",
        "npx playwright test --project=chromium",
        "timeout 30 python3 -u check.py", "UPDATE_SNAPSHOTS=0 npx playwright test",
        "npx playwright test --update-snapshots=false", "npx jest -u", "python3 check.py || true",
        ["sh", "-c", "python3 check.py -u"],
    )
    _CLEAN = {
        "--threshold tightened": ("python3 check.py --threshold 0.05", "python3 check.py --threshold 0.01"),
        "% tightened": ("python3 check.py --threshold 5%", "python3 check.py --threshold 1%"),
        "--flag val -> --flag=val": ("python3 check.py --threshold 0.05", "python3 check.py --threshold=0.01"),
        "prefix tightened": ("THRESHOLD=0.05 python3 check.py", "THRESHOLD=0.01 python3 check.py"),
        "export tightened": ("export THRESHOLD=0.05; python3 check.py", _EXPORTED),
        "sh -c tightened": ("bash -c 'export THRESHOLD=0.05; python3 check.py'", "bash -c '" + _EXPORTED + "'"),
        "compound tightened": ("a --threshold 0.05 && b --threshold 0.02", "a --threshold 0.01 && b --threshold 0.01"),
        "--reporter=list before": ("npx playwright test --threshold 0.01",
                                   "npx playwright test --reporter=list --threshold 0.01"),
        "--color=never after": ("npx playwright test", "npx playwright test --color=never"),
        "trailing --no-color": ("npx playwright test", "npx playwright test --no-color"),
        "trailing --verbose": ("npx playwright test", "npx playwright test --verbose"),
        "trailing -q": ("pytest tests/visual", "pytest tests/visual -q"),
        "python3 -u": ("python3 check.py", "python3 -u check.py"),
        "timeout python3 -u": ("timeout 30 python3 check.py", "timeout 30 python3 -u check.py"),
        "--update-snapshots=false": ("npx playwright test", "npx playwright test --update-snapshots=false"),
        "UPDATE_SNAPSHOTS=0": ("npx playwright test", "UPDATE_SNAPSHOTS=0 npx playwright test"),
        "zero threshold added": ("npx playwright test", "npx playwright test --threshold 0"),
        "comment + whitespace": (_EXPORTED, "export THRESHOLD=0.01;   python3 check.py  # visual check"),
        "newlines": (_EXPORTED, "export THRESHOLD=0.01\npython3 check.py\n"),
        "line continuation": ("python3 check.py --threshold 0.01", "python3 check.py \\\n  --threshold 0.01"),
        "harmless quoting": (_EXPORTED, "export 'THRESHOLD=0.01'; python3 \"check.py\""),
        "argv form": ("python3 check.py", ["python3", "check.py"]),
    }

    def _checked_repo(self, home: Path, command: str) -> Path:
        """A repo whose committed `check.py` predates the committed frozen `visual_diff` harness."""
        from factonio.harness import bootstrap_verify_harness

        repo = _make_repo(home)
        _write(repo, "snapshots/home.png", "png-v1\n")
        _write(repo, "check.py", self._CHECK)
        _commit_all(repo, "visual check script + baseline")
        bootstrap_verify_harness(str(repo), hooks=[
            {"name": "pixels", "kind": "visual_diff", "command": command, "baseline_paths": ["snapshots/**"]},
        ])
        _commit_all(repo, "harness")
        return repo

    def _assert_rejected(self, cases: dict[str, tuple[Any, Any]]) -> None:
        from factonio.harness import _visual_hook_loosening

        for label, (base, live) in cases.items():
            with self.subTest(label):
                self.assertTrue(_visual_hook_loosening({"command": base}, {"command": live}), (base, live))

    def _assert_clean(self, cases: dict[str, tuple[Any, Any]]) -> None:
        from factonio.harness import _visual_hook_loosening

        for label, (base, live) in cases.items():
            with self.subTest(label):
                self.assertEqual(_visual_hook_loosening({"command": base}, {"command": live}), [])

    def test_round12_attack_classes_fail_check_and_audit_end_to_end(self):
        from factonio.harness import audit_harness, check_harness

        self._assert_rejected({live: (self._EXPORTED, live) for live in self._EXECUTED_BYPASSES})
        with isolated_hermes_home() as home:
            repo = self._checked_repo(home, self._EXPORTED)
            committed = check_harness(str(repo), run_hooks=True)
            self.assertEqual(committed["hook_results"]["failed_required"], ["pixels"], committed["hook_results"])
            self.assertEqual(audit_harness(str(repo))["severity"], "clean")
            for live in self._EXECUTED_BYPASSES:
                with self.subTest(live=live):
                    self._set_command(repo, live)
                    chk = check_harness(str(repo), run_hooks=True)
                    self.assertEqual(chk["hook_results"]["failed_required"], [], chk["hook_results"])  # real bypass
                    self.assertFalse(chk["ok"])
                    self.assertFalse(chk["valid"])
                    self.assertEqual(chk["gate_loosening"]["loosened_visual_hooks"], ["pixels"], chk["gate_loosening"])
                    audit = audit_harness(str(repo))
                    self.assertEqual((audit["severity"], audit["gate_loosening"]["loosened"]), ("stale", True))

    def test_parser_level_attacks_are_rejected(self):
        self._assert_rejected(self._REJECTED)

    def test_specific_diagnostics_name_the_loosening(self):
        from factonio.harness import _visual_hook_loosening

        def reasons(base: Any, live: Any) -> list[str]:
            return _visual_hook_loosening({"command": base}, {"command": live})

        renamed = reasons(self._EXPORTED, "export threshold=0.01; python3 check.py")
        self.assertIn("removes threshold `THRESHOLD=0.01` (falls back to the tool default)", renamed)
        self.assertTrue(any("adds threshold `threshold=0.01`" in r for r in renamed), renamed)
        self.assertIn("loosens threshold `--threshold` from 0.01 to 0.5",
                      reasons("python3 check.py --threshold 0.01", "python3 check.py --threshold 0.5"))
        self.assertIn("changes threshold unit `--threshold` (`%` vs plain number) from 1% to 0.05",
                      reasons("python3 check.py --threshold 1%", "python3 check.py --threshold 0.05"))
        self.assertIn("loosens threshold `--threshold` from 1% to 5%",
                      reasons("python3 check.py --threshold 1%", "python3 check.py --threshold 5%"))
        self.assertTrue(any(r.startswith("sets non-finite or unparseable threshold `--threshold`")
                            for r in reasons("x --threshold 0.01", "x --threshold nan")))
        self.assertTrue(any(r.startswith("adds threshold `--threshold=0.5`")
                            for r in reasons("python3 check.py", "python3 check.py --threshold 0.5")))
        self.assertIn("adds snapshot-regeneration flag(s) `-u`", reasons("npx jest", "npx jest -u"))
        self.assertIn("adds snapshot-regeneration flag(s) `UPDATE_SNAPSHOTS=1`",
                      reasons("npx jest", "UPDATE_SNAPSHOTS=1 npx jest"))
        self.assertIn("adds shell `||` fallback to visual_diff hook (a failing check can still pass)",
                      reasons("python3 check.py", "python3 check.py || true"))
        self.assertEqual(reasons("python3 check.py", "python3 check.py 'x"),
                         ["mutates visual_diff command with unparseable shell syntax"])
        self.assertEqual(reasons(self._EXPORTED, "export THRESHOLD=0.01; (unset THRESHOLD); python3 check.py"),
                         ["mutates committed visual_diff command with an unverifiable shell/structural rewrite"])

    def test_unchanged_commands_of_any_shell_complexity_stay_clean(self):
        from factonio.harness import audit_harness, check_harness

        self._assert_clean({json.dumps(cmd): (cmd, cmd) for cmd in self._UNCHANGED})
        self._assert_clean({"malformed but unchanged": ("python3 check.py 'x", "python3 check.py 'x")})
        with isolated_hermes_home() as home:
            for n, cmd in enumerate(self._UNCHANGED[:4]):
                with self.subTest(cmd=cmd):
                    repo = self._visual_repo(home, cmd, name=f"svc{n}")
                    self._set_command(repo, cmd)
                    chk = check_harness(str(repo))
                    self.assertTrue(chk["valid"], chk["errors"])
                    self.assertFalse(chk["gate_loosening"]["loosened"], chk["gate_loosening"])
                    self.assertEqual(audit_harness(str(repo))["severity"], "clean")

    def test_in_place_tightening_and_inert_additions_stay_clean(self):
        from factonio.harness import audit_harness, check_harness

        self._assert_clean(self._CLEAN)
        with isolated_hermes_home() as home:
            for n, label in enumerate(("export tightened", "sh -c tightened", "--reporter=list before",
                                       "python3 -u", "UPDATE_SNAPSHOTS=0", "comment + whitespace")):
                base, live = self._CLEAN[label]
                with self.subTest(label):
                    repo = self._visual_repo(home, base, name=f"svc{n}")
                    self._set_command(repo, live)
                    chk = check_harness(str(repo))
                    self.assertTrue(chk["valid"], chk["errors"])
                    self.assertFalse(chk["gate_loosening"]["loosened"], chk["gate_loosening"])
                    audit = audit_harness(str(repo))
                    self.assertEqual((audit["severity"], audit["gate_loosening"]["loosened"]), ("clean", False))

    def test_scope_preserving_shell_rewrites_fail_closed(self):
        """Injecting subshells, unsets or env mutators into a committed command is rejected even when it would keep
        the effective threshold: the guard does not emulate the shell. Committing the same command is clean."""
        from factonio.harness import _visual_hook_loosening, check_harness

        rewrites = ("(unset THRESHOLD); python3 check.py", "(export THRESHOLD=0.9); python3 check.py",
                    "export -n THRESHOLD; export THRESHOLD; python3 check.py", "unset -fv FOO; python3 check.py")
        with isolated_hermes_home() as home:
            repo = self._checked_repo(home, self._EXPORTED)
            for live in (f"export THRESHOLD=0.01; {r}" for r in rewrites):
                with self.subTest(live=live):
                    self._set_command(repo, live)
                    chk = check_harness(str(repo), run_hooks=True)
                    self.assertEqual(chk["hook_results"]["failed_required"], ["pixels"])  # the bar still holds...
                    self.assertTrue(chk["gate_loosening"]["loosened"])  # ...but the rewrite is unverifiable
                    self.assertTrue(any(self._UNVERIFIABLE in f for f in chk["gate_loosening"]["findings"]))
                    self.assertEqual(_visual_hook_loosening({"command": live}, {"command": live}), [])

    def test_tokenizer_normalizes_only_inert_syntax(self):
        from factonio.harness import _tokenize_visual_cmd

        def sig(cmd: Any) -> list[tuple[str, bool]]:
            return [(t.text, t.is_op) for t in _tokenize_visual_cmd(cmd)]

        self.assertEqual(sig("a 'b c' \"d\" # e --threshold 9\n\nf;;"), sig("a \"b c\" d; f;;"))
        self.assertEqual(sig("x --color=#fff y#z"), [("x", False), ("--color=#fff", False), ("y#z", False)])
        self.assertEqual(sig("cmd 2>&1 | tee log"), [("cmd", False), ("2>&", True), ("1", False), ("|", True),
                                                      ("tee", False), ("log", False)])
        self.assertEqual(sig("{ a; } && (b)"), [("{", True), ("a", False), (";", True), ("}", True),
                                                 ("&&", True), ("(", True), ("b", False), (")", True)])
        self.assertEqual(sig("${X} a{b,c}"), [("${X}", False), ("a{b,c}", False)])
        for bad in ("a 'b", 'a "b', "a \\"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                _tokenize_visual_cmd(bad)
        self.assertEqual(sig(["sh", "-c", "a; b"]), [("sh", False), ("-c", False), ("a; b", False)])



class TestFixRound13StructuralGuardHardening(unittest.TestCase):
    """Round 13: a positive allowlist of inert insertions, exact flag spelling, strict ASCII threshold syntax,
    preserved outer quoting of `sh -c` scripts / argv lists, and operator-adjacent whitespace."""

    _set_command = staticmethod(TestFixRound2VisualGuardAndHarnessGaps._set_command)
    _assert_rejected = TestFailClosedStructuralVisualHookPolicy._assert_rejected
    _assert_clean = TestFailClosedStructuralVisualHookPolicy._assert_clean
    # Visual diff: ratio 0.25 / 100 pixels; the lenient defaults pass it. Runner-style options that skip
    # collection or approve/regenerate snapshots exit 0, as `pytest --co`, `--fixtures` or `--approve` would.
    _PY_CHECK = (
        "import argparse, os, sys\n"
        "SKIP = {'--co', '--collect-only', '--fixtures', '--markers', '--setup-plan', '--approve', '--bless',\n"
        "        '--version', '-V', '-h', '--help', '-run=NEVER'}\n"
        "if any(a in SKIP or a.split('=')[0] in ('--override-ini', '--mode', '--project') for a in sys.argv[1:]):\n"
        "    sys.exit(0)\n"
        "p = argparse.ArgumentParser(add_help=False)\n"
        "p.add_argument('--threshold', type=float, default=1.0)\n"
        "p.add_argument('--max-diff-pixels', type=int, default=10 ** 9)\n"
        "a, _ = p.parse_known_args()\n"
        "t = float(os.environ.get('THRESHOLD') or a.threshold)\n"
        "sys.exit(0 if 0.25 <= t and 100 <= a.max_diff_pixels else 1)\n"
    )
    # Visual diff 0.80 against a JS `Number(...)` threshold: NaN makes `0.80 > t` false, so the check passes.
    _JS_CHECK = (
        "const args = process.argv.slice(2);\n"
        "let raw = process.env.THRESHOLD;\n"
        "for (let i = 0; i < args.length; i++) {\n"
        "  if (args[i] === '--threshold') raw = args[i + 1];\n"
        "  else if (args[i].startsWith('--threshold=')) raw = args[i].slice(12);\n"
        "}\n"
        "if (raw === undefined) raw = '1';\n"
        "const t = raw.endsWith('%') ? Number(raw.slice(0, -1)) / 100 : Number(raw);\n"
        "process.exit(0.80 > t ? 1 : 0);\n"
    )

    def _repo(self, home: Path, name: str, command: Any) -> Path:
        from factonio.harness import bootstrap_verify_harness

        repo = _make_repo(home, name)
        _write(repo, "snapshots/home.png", "png-v1\n")
        _write(repo, "check.py", self._PY_CHECK)
        _write(repo, "check.js", self._JS_CHECK)
        _commit_all(repo, "visual checks + baseline")
        bootstrap_verify_harness(str(repo), hooks=[
            {"name": "pixels", "kind": "visual_diff", "command": command, "baseline_paths": ["snapshots/**"]},
        ])
        _commit_all(repo, "harness")
        return repo

    def _assert_executed_bypasses_rejected(self, pairs: list[tuple[Any, Any]]) -> None:
        """Each committed command fails its hook; each mutation really passes it, yet check and audit flag it."""
        from factonio.harness import audit_harness, check_harness

        with isolated_hermes_home() as home:
            for n, (base, live) in enumerate(pairs):
                with self.subTest(base=base, live=live):
                    repo = self._repo(home, f"svc{n}", base)
                    committed = check_harness(str(repo), run_hooks=True)
                    self.assertEqual(committed["hook_results"]["failed_required"], ["pixels"])
                    self._set_command(repo, live)
                    chk = check_harness(str(repo), run_hooks=True)
                    self.assertEqual(chk["hook_results"]["failed_required"], [], chk["hook_results"])  # real bypass
                    self.assertFalse(chk["valid"])
                    self.assertEqual(chk["gate_loosening"]["loosened_visual_hooks"], ["pixels"], chk["gate_loosening"])
                    audit = audit_harness(str(repo))
                    self.assertEqual((audit["severity"], audit["gate_loosening"]["loosened"]), ("stale", True))

    def test_only_allowlisted_inert_options_may_be_inserted(self):
        self._assert_rejected({
            "--override-ini snapshot-update": ("pytest tests/visual",
                                               "pytest tests/visual --override-ini=addopts=--snapshot-update"),
            "--override-ini collect-only": ("pytest tests/visual",
                                            "pytest tests/visual --override-ini=addopts=--collect-only"),
            "--co": ("pytest tests/visual", "pytest tests/visual --co"),
            "--fixtures": ("pytest tests/visual", "pytest tests/visual --fixtures"),
            "--markers": ("pytest tests/visual", "pytest tests/visual --markers"),
            "--setup-plan": ("pytest tests/visual", "pytest tests/visual --setup-plan"),
            "go -run=NEVER": ("go test ./...", "go test ./... -run=NEVER"),
            "--mode=update": ("npx vitest run", "npx vitest run --mode=update"),
            "--approve": ("python3 check.py", "python3 check.py --approve"),
            "--bless": ("cargo test", "cargo test --bless"),
            "--project=empty": ("npx playwright test", "npx playwright test --project=empty"),
            "--config=": ("npx playwright test", "npx playwright test --config=empty.config.ts"),
            "--version": ("pytest tests/visual", "pytest tests/visual --version"),
            "-V": ("pytest tests/visual", "pytest tests/visual -V"),
            "-h": ("pytest tests/visual", "pytest tests/visual -h"),
            "unknown --flag=value": ("npx playwright test", "npx playwright test --workers=1"),
            "unknown trailing flag": ("npx playwright test", "npx playwright test --headed"),
            "node -v": ("node check.js", "node -v check.js"),
            "node trailing -v": ("node check.js", "node check.js -v"),
            "jest -v is --version": ("npx jest", "npx jest -v"),
            "vitest -v is --version": ("npx vitest run", "npx vitest run -v"),
            "-q on an unknown script": ("python3 check.py", "python3 check.py -q"),
            "-v not trailing": ("pytest tests/visual", "pytest -v tests/visual"),
            "python3 check.py -u": ("python3 check.py", "python3 check.py -u"),
            "--reporter path": ("npx playwright test", "npx playwright test --reporter=./reporter.js"),
            "--reporter plugin": ("npx playwright test", "npx playwright test --reporter=evil"),
            "--color expansion": ("npx playwright test", "npx playwright test --color=$C"),
            "--verbose not trailing": ("npx playwright test --threshold 0.01",
                                       "npx playwright test --verbose --threshold 0.01"),
            "swallowed by -run": ("go test -run TestVisual ./...", "go test -run --color=never TestVisual ./..."),
            "UPDATE_SNAPSHOTS=$X": ("npx jest", "UPDATE_SNAPSHOTS=$X npx jest"),
        })
        self._assert_clean({
            "pytest -q": ("pytest tests/visual", "pytest tests/visual -q"),
            "pytest -v": ("pytest tests/visual", "pytest tests/visual -v"),
            "uv run pytest -q": ("uv run pytest tests/visual", "uv run pytest tests/visual -q"),
            "go -v": ("go test ./...", "go test ./... -v"),
            "--reporter=list": ("npx playwright test", "npx playwright test --reporter=list"),
            "--color=never": ("npx playwright test", "npx playwright test --color=never"),
            "--verbose": ("npx playwright test", "npx playwright test --verbose"),
            "--no-color": ("npx jest", "npx jest --no-color"),
            "--silent": ("npx jest", "npx jest --silent"),
            "python3 -u": ("python3 check.py", "python3 -u check.py"),
            "UPDATE_SNAPSHOTS=0": ("npx playwright test", "UPDATE_SNAPSHOTS=0 npx playwright test"),
            "--update-snapshots=false": ("npx playwright test", "npx playwright test --update-snapshots=false"),
            "unchanged --project": ("npx playwright test --project=chromium", "npx playwright test --project=chromium"),
        })
        self._assert_executed_bypasses_rejected([
            ("python3 check.py tests/visual --threshold 0.01",
             f"python3 check.py tests/visual --threshold 0.01 {opt}")
            for opt in ("--override-ini=addopts=--collect-only", "--co", "--fixtures", "--markers", "--setup-plan",
                        "-run=NEVER", "--mode=update", "--approve", "--bless", "--project=empty")
        ])

    def test_flag_thresholds_keep_their_exact_spelling(self):
        self._assert_rejected({
            "--THRESHOLD": ("python3 check.py --threshold 0.01", "python3 check.py --THRESHOLD 0.01"),
            "---threshold": ("python3 check.py --threshold 0.01", "python3 check.py ---threshold 0.01"),
            "-threshold": ("python3 check.py --threshold 0.01", "python3 check.py -threshold 0.01"),
            "--max_diff_pixels": ("python3 check.py --max-diff-pixels=0", "python3 check.py --max_diff_pixels=0"),
        })
        self._assert_clean({
            "--flag val -> --flag=val": ("python3 check.py --threshold 0.05", "python3 check.py --threshold=0.01"),
            "pixels tightened": ("python3 check.py --max-diff-pixels=5", "python3 check.py --max-diff-pixels 0"),
        })
        self._assert_executed_bypasses_rejected([
            ("python3 check.py --threshold 0.01", "python3 check.py --THRESHOLD 0.01"),
            ("python3 check.py --threshold 0.01", "python3 check.py ---threshold 0.01"),
            ("python3 check.py --threshold 0.01", "python3 check.py -threshold 0.01"),
            ("python3 check.py --max-diff-pixels=0", "python3 check.py --max_diff_pixels=0"),
        ])

    def test_parse_value_accepts_only_strict_ascii_decimal_syntax(self):
        from factonio.harness import _parse_value

        for raw, value in (("0", 0.0), ("0.05", 0.05), (".01", 0.01), ("1%", 0.01), ("10%", 0.1),
                           ("1e-2", 0.01), ("5e-3", 0.005), ("1E+0", 1.0)):
            with self.subTest(raw=raw):
                self.assertAlmostEqual(_parse_value(raw)[0], value)
        for raw in ("0.0_1", "0_1%", "\uff10.\uff10\uff11", "\u0660.\u0660\u0661", "\uff11%", "+0.01", "-0.01",
                    "NaN", "inf", "1.", "01", " 0.01", "0.01 ", "0x1", "1e", "%", ""):
            with self.subTest(raw=raw):
                self.assertIsNone(_parse_value(raw)[0])

    @unittest.skipUnless(shutil.which("node"), "node is required to execute the JS visual checker")
    def test_thresholds_that_are_nan_in_node_are_rejected_end_to_end(self):
        pairs: list[tuple[Any, Any]] = []
        for v in ("0.0_1", "\uff10.\uff10\uff11", "\u0660.\u0660\u0661"):
            pairs += [
                ("node check.js --threshold 0.05", f"node check.js --threshold {v}"),
                ("node check.js --threshold=0.05", f"node check.js --threshold={v}"),
                ("THRESHOLD=0.05 node check.js", f"THRESHOLD={v} node check.js"),
                ("export THRESHOLD=0.05; node check.js", f"export THRESHOLD={v}; node check.js"),
                ("bash -c 'node check.js --threshold 0.05'", f"bash -c 'node check.js --threshold {v}'"),
            ]
        pairs += [("node check.js --threshold 5%", f"node check.js --threshold {v}") for v in ("0_1%", "\uff11%")]
        self._assert_executed_bypasses_rejected(pairs)
        self._assert_clean({v: ("node check.js --threshold 0.05", f"node check.js --threshold {v}")
                            for v in ("0.01", ".01", "1e-2")})
        self._assert_clean({"unchanged odd literal": ("node check.js --threshold 0.0_1",
                                                      "node check.js --threshold 0.0_1")})

    def test_outer_quoting_of_sh_c_scripts_and_argv_lists_is_preserved(self):
        single = "bash -c 'T=0.01; export THRESHOLD=$T; python3 check.py'"
        double = 'bash -c "T=0.01; export THRESHOLD=$T; python3 check.py"'
        argv = ["bash", "-c", "T=0.01; export THRESHOLD=$T; python3 check.py"]
        self._assert_rejected({
            "single -> double quotes": (single, double),
            "argv list -> double quotes": (argv, double),
            "argv list -> unquoted glob": (["python3", "check.py", "*.png"], "python3 check.py *.png"),
            "string -> argv with spaces": ("python3 check.py 'a b'", ["python3", "check.py", "a b"]),
        })
        # `T` is not a threshold name, so `T=0.05` -> `T=0.01` is an unverifiable edit; a threshold-named
        # variable is tightened in place inside the single-quoted script.
        self._assert_clean({
            "single-quoted tightening": (
                "bash -c 'T_THRESHOLD=0.05; export THRESHOLD=$T_THRESHOLD; python3 check.py'",
                "bash -c 'T_THRESHOLD=0.01; export THRESHOLD=$T_THRESHOLD; python3 check.py'"),
            "double quotes without expansions": ("bash -c 'export THRESHOLD=0.05; python3 check.py'",
                                                 'bash -c "export THRESHOLD=0.01; python3 check.py"'),
            "argv list -> plain string": (["python3", "check.py", "--threshold", "0.05"],
                                          "python3 check.py --threshold 0.01"),
            "unchanged double-quoted": (double, double),
            "unchanged argv list": (argv, argv),
        })
        self._assert_executed_bypasses_rejected([(single, double), (argv, double)])

    def test_whitespace_before_an_operator_is_inert(self):
        from factonio.harness import _tokenize_visual_cmd

        self._assert_clean({
            "space before ;": ("export T=0.01; echo $T; python3 check.py",
                               "export T=0.01; echo $T ; python3 check.py"),
            "space before &&": ("echo $T&& python3 check.py", "echo $T && python3 check.py"),
            "space before |": ("echo $T| cat", "echo $T | cat"),
        })
        self.assertEqual([t.raw for t in _tokenize_visual_cmd("echo $T; x 2>err")],
                         ["echo", "$T", ";", "x", "2>", "err"])
        self.assertTrue(all(t.from_argv for t in _tokenize_visual_cmd(["a", "$T"])))
        self.assertFalse(any(t.from_argv for t in _tokenize_visual_cmd("a $T")))


class TestFixRound14ShellWordsQuotingUnitsAndWrappers(unittest.TestCase):
    """Round 14: `\\r` stays inside a shell word, `sh -c` outer quoting is preserved in both directions, a
    threshold keeps its committed unit (`%` vs plain), and Python `-u` needs a real interpreter/wrapper head."""

    _set_command = staticmethod(TestFixRound2VisualGuardAndHarnessGaps._set_command)
    _assert_rejected = TestFailClosedStructuralVisualHookPolicy._assert_rejected
    _assert_clean = TestFailClosedStructuralVisualHookPolicy._assert_clean
    _assert_executed_bypasses_rejected = TestFixRound13StructuralGuardHardening._assert_executed_bypasses_rejected
    # A repo-relative executable that "regenerates" (exits 0) on `-u`, as `vitest -u` / `jest -u` would.
    # Visual diff 0.80 against a bare `Number(raw)` threshold (no `%` support): NaN passes the check.
    _PLAIN_JS_CHECK = (
        "const args = process.argv.slice(2);\n"
        "let raw = '1';\n"
        "for (let i = 0; i < args.length; i++) {\n"
        "  if (args[i] === '--threshold') raw = args[i + 1];\n"
        "  else if (args[i].startsWith('--threshold=')) raw = args[i].slice(12);\n"
        "}\n"
        "process.exit(0.80 > Number(raw) ? 1 : 0);\n"
    )
    _REGEN_EXE = '#!/bin/sh\nfor a in "$@"; do [ "$a" = -u ] && exit 0; done\nexit 1\n'

    def _repo(self, home: Path, name: str, command: Any) -> Path:
        from factonio.harness import bootstrap_verify_harness

        repo = _make_repo(home, name)
        _write(repo, "snapshots/home.png", "png-v1\n")
        _write(repo, "check.py", TestFixRound13StructuralGuardHardening._PY_CHECK)
        _write(repo, "check.js", TestFixRound13StructuralGuardHardening._JS_CHECK)
        _write(repo, "plain.js", self._PLAIN_JS_CHECK)
        for rel in ("vitest", "tests/python3"):
            _write(repo, rel, self._REGEN_EXE)
            (repo / rel).chmod(0o755)
        _commit_all(repo, "visual checks + baseline")
        bootstrap_verify_harness(str(repo), hooks=[
            {"name": "pixels", "kind": "visual_diff", "command": command, "baseline_paths": ["snapshots/**"]},
        ])
        _commit_all(repo, "harness")
        return repo

    def test_carriage_return_is_part_of_the_shell_word(self):
        from factonio.harness import _tokenize_visual_cmd

        self.assertEqual([t.text for t in _tokenize_visual_cmd("a --threshold\r 0.01\r#c")],
                         ["a", "--threshold\r", "0.01\r#c"])
        self._assert_rejected({
            "python flag": ("python3 check.py --threshold 0.01", "python3 check.py --threshold\r 0.01"),
            "value + comment": ("node check.js --threshold 0.01", "node check.js --threshold 0.01\r#comment"),
            "bash -c flag": ("bash -c 'python3 check.py --threshold 0.01'",
                             "bash -c 'python3 check.py --threshold\r 0.01'"),
        })
        pairs: list[tuple[Any, Any]] = [
            ("python3 check.py --threshold 0.01", "python3 check.py --threshold\r 0.01"),
            ("bash -c 'python3 check.py --threshold 0.01'", "bash -c 'python3 check.py --threshold\r 0.01'"),
        ]
        if shutil.which("node"):  # Node's `Number("0.01\r#comment")` / `Number("1%\r")` is NaN
            pairs += [
                ("node check.js --threshold 0.01", "node check.js --threshold\r 0.01"),
                ("node check.js --threshold 0.01", "node check.js --threshold 0.01\r#comment"),
                ("node check.js --threshold=1% --color=never", "node check.js --threshold=1%\r --color=never"),
            ]
        self._assert_executed_bypasses_rejected(pairs)

    def test_sh_c_outer_quoting_is_preserved_in_both_directions(self):
        flips = {
            "${T:-...} double -> single": ('T=0.01; bash -c "python3 check.py --threshold ${T:-0.99}"',
                                           "T=0.01; bash -c 'python3 check.py --threshold ${T:-0.99}'"),
            "export $T double -> single": ('T=0.01; bash -c "export THRESHOLD=$T; python3 check.py"',
                                           "T=0.01; bash -c 'export THRESHOLD=$T; python3 check.py'"),
            "sh -c": ('T=0.01; sh -c "python3 check.py --threshold ${T:-0.99}"',
                      "T=0.01; sh -c 'python3 check.py --threshold ${T:-0.99}'"),
            "bash -ec": ('T=0.01; bash -ec "python3 check.py --threshold ${T:-0.99}"',
                         "T=0.01; bash -ec 'python3 check.py --threshold ${T:-0.99}'"),
            "$(...)": ('T=0.01; bash -c "python3 check.py --threshold $(echo ${T:-0.99})"',
                       "T=0.01; bash -c 'python3 check.py --threshold $(echo ${T:-0.99})'"),
            "backticks": ('T=0.01; bash -c "python3 check.py --threshold `echo ${T:-0.99}`"',
                          "T=0.01; bash -c 'python3 check.py --threshold `echo ${T:-0.99}`'"),
            "unquoted -> single": ("T=0.01; bash -c python3\\ check.py\\ --threshold\\ ${T:-0.99}",
                                   "T=0.01; bash -c 'python3 check.py --threshold ${T:-0.99}'"),
            "nested sh -c bash -c": ('T=0.01; sh -c "bash -c \'python3 check.py --threshold ${T:-0.99}\'"',
                                     "T=0.01; sh -c 'bash -c \"python3 check.py --threshold ${T:-0.99}\"'"),
        }
        self._assert_rejected(flips)
        self._assert_rejected({
            "positional after an earlier -c": ("bash -c 'python3 check.py' -c 'x --threshold 0.05'",
                                               "bash -c 'python3 check.py' -c 'x --threshold 0.01'"),
            "option with a value": ("bash -o pipefail -c 'python3 check.py --threshold 0.05'",
                                    "bash -o pipefail -c 'python3 check.py --threshold 0.01'"),
        })
        self._assert_clean({
            "single-quoted tightening": ("bash -c 'python3 check.py --threshold 0.05'",
                                         "bash -c 'python3 check.py --threshold 0.01'"),
            "bash -e -c tightening": ("bash -e -c 'python3 check.py --threshold 0.05'",
                                      "bash -e -c 'python3 check.py --threshold 0.01'"),
            "unchanged expanding script": (flips["sh -c"][0], flips["sh -c"][0]),
        })
        self._assert_executed_bypasses_rejected(list(flips.values()))

    def test_threshold_keeps_its_committed_unit(self):
        self._assert_rejected({
            "plain -> %": ("node check.js --threshold 0.05", "node check.js --threshold 1%"),
            "% -> plain": ("node check.js --threshold 5%", "node check.js --threshold 0.01"),
            "env plain -> %": ("THRESHOLD=0.05 node check.js", "THRESHOLD=1% node check.js"),
        })
        self._assert_clean({
            "plain tightened": ("node check.js --threshold 0.05", "node check.js --threshold 0.01"),
            "% tightened": ("node check.js --threshold 5%", "node check.js --threshold 1%"),
        })

    @unittest.skipUnless(shutil.which("node"), "node is required to execute the JS visual checker")
    def test_unit_change_that_is_nan_in_node_fails_end_to_end(self):
        """`plain.js` reads `Number(raw)`: `Number("1%")` is NaN and `0.80 > NaN` is false, so the check passes."""
        from factonio.harness import audit_harness, check_harness

        self._assert_executed_bypasses_rejected([
            ("node plain.js --threshold 0.05", "node plain.js --threshold 1%"),
            ("node plain.js --threshold=0.05", "node plain.js --threshold=1%"),
        ])
        with isolated_hermes_home() as home:
            for n, (base, live, loosened) in enumerate((
                    ("node plain.js --threshold 5%", "node plain.js --threshold 0.01", True),
                    ("node plain.js --threshold 0.05", "node plain.js --threshold 0.01", False),
                    ("node check.js --threshold=5%", "node check.js --threshold=1%", False))):
                with self.subTest(base=base, live=live):
                    repo = self._repo(home, f"svc{n}", base)
                    self._set_command(repo, live)
                    chk = check_harness(str(repo), run_hooks=True)
                    self.assertEqual(chk["hook_results"]["failed_required"], ["pixels"])  # 0.80 > 0.01 either way
                    self.assertEqual(chk["gate_loosening"]["loosened"], loosened, chk["gate_loosening"])
                    audit = audit_harness(str(repo))
                    self.assertEqual(audit["gate_loosening"]["loosened"], loosened)
                    self.assertEqual(audit["severity"], "stale" if loosened else "clean")

    def test_python_u_needs_a_real_interpreter_or_wrapper_head(self):
        bypasses = [
            ("./vitest run tests/python3 tests/visual.test.ts", "./vitest run tests/python3 -u tests/visual.test.ts"),
            ("./vitest -t python3 tests/visual.test.ts", "./vitest -t python3 -u tests/visual.test.ts"),
            ("timeout 30 tests/python3 check.py", "timeout 30 tests/python3 -u check.py"),
        ]
        self._assert_rejected({live: (base, live) for base, live in bypasses})
        self._assert_rejected({
            "relative wrapper": ("tests/timeout 30 python3 check.py", "tests/timeout 30 python3 -u check.py"),
            "uv without run": ("uv tool python3 check.py", "uv tool python3 -u check.py"),
        })
        self._assert_clean({
            "python3 -u": ("python3 check.py", "python3 -u check.py"),
            "/usr/bin/python3 -u": ("/usr/bin/python3 check.py", "/usr/bin/python3 -u check.py"),
            "timeout python3 -u": ("timeout 30 python3 check.py", "timeout 30 python3 -u check.py"),
            "uv run python3 -u": ("uv run python3 check.py", "uv run python3 -u check.py"),
            "nice -n 5 python3 -u": ("nice -n 5 python3 check.py", "nice -n 5 python3 -u check.py"),
        })
        self._assert_executed_bypasses_rejected(bypasses)


if __name__ == "__main__":
    unittest.main()
