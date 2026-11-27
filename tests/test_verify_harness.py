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
            "plain -> none (separate)": ("", "--update-snapshots none", False),
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
    """Shell-aware visual_diff guard, post-hook baseline re-check, verify.md hook validation, audit gate base."""

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

    def test_shell_wrapped_and_punctuation_adjacent_commands(self):
        from factonio.harness import check_harness, detect_harness_gate_loosening

        pw, jest, thr = "npx playwright test", "npx jest", "true --threshold 0.01"
        cases = {
            "sh -c string": (f"sh -c '{pw}'", f'sh -c "{pw} --update-snapshots"', True),
            "bash -lc jest -u": (jest, "bash -lc 'npx jest -u'", True),
            "bash -euo pipefail -c": (pw, f"bash -euo pipefail -c '{pw} --update-snapshots'", True),
            "argv sh -c": (["sh", "-c", pw], ["sh", "-c", f"{pw} --update-snapshots"], True),
            "argv /bin/bash -ec": (pw, ["/bin/bash", "-ec", f"{pw} -u"], True),
            "nested sh in bash": (pw, f"""bash -c "sh -c '{pw} --update-snapshots'" """, True),
            "; after flag": (pw, f"{pw} --update-snapshots; echo ok", True),
            "&& glued": (pw, f"{pw} --update-snapshots&&true", True),
            "subshell": (pw, f"({pw} --update-snapshots)", True),
            "jest -u;": (jest, "npx jest -u; echo ok", True),
            "env -S": (pw, f"env -S '{pw} -u'", True),
            "threshold sh -c": (thr, 'sh -c "true --threshold 0.5"', True),
            "threshold ;": (thr, "true --threshold 0.5; echo ok", True),
            "threshold removed": (thr, "true", True),
            "threshold removed in sh -c": (thr, "sh -c 'true --update-snapshots=none'", True),
            "=none ;": (pw, f"{pw} --update-snapshots=none; echo ok", False),
            "none separate ;": (pw, f"{pw} --update-snapshots none; echo ok", False),
            "env none ;": (pw, f"UPDATE_SNAPSHOTS=none; {pw}", False),
            "env 0 ;": (pw, f"UPDATE_SNAPSHOTS=0; {pw}", False),
            "sh -c env 0": (pw, f"sh -c 'UPDATE_SNAPSHOTS=0 {pw}'", False),
            "sh -c env none": (pw, f"sh -c 'UPDATE_SNAPSHOTS=none {pw}'", False),
            "bash -u is nounset": (pw, f"bash -u -c '{pw}'", False),
            "env -u unsets": (pw, f"env -u CI {pw}", False),
            "same threshold wrapped": (thr, "sh -c 'true --threshold 0.01'", False),
            "tightened threshold ;": (thr, "true --threshold 0.001; echo ok", False),
        }
        with isolated_hermes_home() as home:
            repos: dict[str, Path] = {}
            for label, (base, live, loosened) in cases.items():
                with self.subTest(label):
                    key = json.dumps(base)
                    if key not in repos:
                        repos[key] = self._visual_repo(home, base, name=f"svc{len(repos)}")
                    repo = repos[key]
                    self._set_command(repo, live)
                    gate = detect_harness_gate_loosening(repo)
                    self.assertEqual(gate["loosened"], loosened, gate["findings"])
                    self.assertEqual(gate["loosened_visual_hooks"], ["pixels"] if loosened else [])
                    if label in ("sh -c string", "threshold removed", "sh -c env none"):
                        chk = check_harness(str(repo))
                        self.assertEqual(chk["valid"], not loosened, chk["errors"])
            self._set_command(repos[json.dumps(thr)], "true")
            removed = detect_harness_gate_loosening(repos[json.dumps(thr)])
            self.assertTrue(any("removes threshold `threshold=0.01`" in f for f in removed["findings"]), removed)

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


class TestFixRound4CompoundThresholdsAndShortFlags(unittest.TestCase):
    """Every threshold occurrence in compound commands counts; short `-u` is executable-aware and case-sensitive."""

    _visual_repo = staticmethod(TestFixRound2VisualGuardAndHarnessGaps._visual_repo)
    _set_command = staticmethod(TestFixRound2VisualGuardAndHarnessGaps._set_command)

    def _assert_cases(self, cases: dict[str, tuple[Any, Any, bool]]) -> None:
        from factonio.harness import detect_harness_gate_loosening

        with isolated_hermes_home() as home:
            repos: dict[str, Path] = {}
            for label, (base, live, loosened) in cases.items():
                with self.subTest(label):
                    key = json.dumps(base)
                    if key not in repos:
                        repos[key] = self._visual_repo(home, base, name=f"svc{len(repos)}")
                    self._set_command(repos[key], live)
                    gate = detect_harness_gate_loosening(repos[key])
                    self.assertEqual(gate["loosened"], loosened, gate["findings"])
                    self.assertEqual(gate["loosened_visual_hooks"], ["pixels"] if loosened else [])

    def test_compound_threshold_occurrences_are_all_compared(self):
        a, b = "python3 check_snap.py a", "python3 check_snap.py b"
        same = f"{a} --threshold 0.01 && {b} --threshold 0.01"
        mixed = f"{a} --threshold 0.01 && {b} --threshold 0.05"
        single = "python3 check_ui.py --threshold 0.01"
        self._assert_cases({
            "earlier raised &&": (same, f"{a} --threshold 0.50 && {b} --threshold 0.01", True),
            "earlier raised ;": (same, f"{a} --threshold 0.50; {b} --threshold 0.01", True),
            "earlier raised newline": (same, f"{a} --threshold 0.50\n{b} --threshold 0.01", True),
            "earlier raised sh -c": (same, f"sh -c '{a} --threshold 0.50 && {b} --threshold 0.01'", True),
            "earlier raised below later": (mixed, f"{a} --threshold 0.04 && {b} --threshold 0.05", True),
            "swapped": (mixed, f"{a} --threshold 0.05 && {b} --threshold 0.01", True),
            "dummy trailing threshold": (single, "python3 check_ui.py --threshold 0.50 && true --threshold 0.01", True),
            "extra dummy hides raise": (mixed, f"{a} --threshold 0.04 && {b} --threshold 0.05 && true --threshold 0.01",
                                        True),
            "earlier removed": (same, f"{a} && {b} --threshold 0.01", True),
            "earlier nan": (same, f"{a} --threshold nan && {b} --threshold 0.01", True),
            "earlier inf": (same, f"{a} --threshold inf && {b} --threshold 0.01", True),
            "earlier -inf sh -c": (same, f"sh -c '{a} --threshold -inf; {b} --threshold 0.01'", True),
            "nan beside unchanged": (same, f"{same} && true --threshold nan", True),
            "unchanged": (same, same, False),
            "tightened earlier": (f"{a} --threshold 0.05 && {b} --threshold 0.05",
                                  f"{a} --threshold 0.01 && {b} --threshold 0.05", False),
            "added stricter check": (mixed, f"{mixed} && python3 check_snap.py c --threshold 0.001", False),
        })

    def test_short_u_is_executable_aware_and_case_sensitive(self):
        base, pw = "true", "npx playwright test"
        self._assert_cases({
            "python3 -u": (base, "python3 -u scripts/check_ui.py", False),
            "argv python3 -u": (base, ["python3", "-u", "scripts/check_ui.py"], False),
            "python -u": (base, "python -u scripts/check_ui.py", False),
            "python3.14 -u": (base, "python3.14 -u scripts/check_ui.py", False),
            "argv /usr/bin/python3.14 -u": (base, ["/usr/bin/python3.14", "-u", "scripts/check_ui.py"], False),
            "python3 -u -m pytest": (base, "python3 -u -m pytest tests/test_ui.py", False),
            "python3 -X dev -u": (base, "env CI=1 python3 -X dev -u scripts/check_ui.py", False),
            "sh -c python3 -u": (base, "sh -c 'python3 -u check.py'", False),
            "bash -u script": (base, "bash -u scripts/check_ui.sh", False),
            "set -e -u": (base, f"sh -c 'set -e -u; {pw}'", False),
            "set -euo pipefail newline": (base, f"set -euo pipefail\n{pw}", False),
            "diff -u": (base, "diff -u a.txt b.txt", False),
            "-U is not -u": (base, "npx jest -U", False),
            "jest -u": (base, "jest -u", True),
            "vitest -u": (base, "vitest -u", True),
            "npx jest -u": (base, "npx jest -u", True),
            "npx vitest -u": (base, "npx vitest -u", True),
            "npm test -- -u": (base, "npm test -- -u", True),
            "--update-snapshots": (base, f"{pw} --update-snapshots", True),
            "--UPDATE-SNAPSHOTS": (base, f"{pw} --UPDATE-SNAPSHOTS", True),
            "python script --update-snapshots": (base, "python3 -u scripts/check_ui.py --update-snapshots", True),
            "python -m pytest --snapshot-update": (base, "python3 -u -m pytest --snapshot-update", True),
            "python script -u": (base, "python3 scripts/check_ui.py -u", True),
            "bash script -u": (base, "bash scripts/check_ui.sh -u", True),
            "set -u then jest -u": (base, "sh -c 'set -u; npx jest -u'", True),
            "diff -u then jest -u": (base, "diff -u a b && npx jest -u", True),
        })

    def test_compound_threshold_loosening_fails_check_and_audit(self):
        from factonio.harness import audit_harness, check_harness

        script = ("import sys\n"
                  "diff = {'a': 0.25, 'b': 0.0}[sys.argv[1]]\n"
                  "sys.exit(1 if diff > float(sys.argv[sys.argv.index('--threshold') + 1]) else 0)\n")
        a, b = "python3 check_snap.py a", "python3 check_snap.py b"
        with isolated_hermes_home() as home:
            repo = self._visual_repo(home, f"{a} --threshold 0.01 && {b} --threshold 0.01", name="svc")
            _write(repo, "check_snap.py", script)
            _commit_all(repo, "visual check script")
            failing = check_harness(str(repo), run_hooks=True)
            self.assertEqual(failing["hook_results"]["failed_required"], ["pixels"], failing["hook_results"])
            self._set_command(repo, f"{a} --threshold 0.50 && {b} --threshold 0.01")
            chk = check_harness(str(repo), run_hooks=True)
            self.assertEqual(chk["hook_results"]["failed_required"], [])
            self.assertFalse(chk["valid"])
            self.assertTrue(chk["gate_loosening"]["loosened"])
            self.assertTrue(any("loosens threshold `threshold` from 0.01 to 0.5" in e for e in chk["errors"]),
                            chk["errors"])
            audit = audit_harness(str(repo))
            self.assertEqual((audit["severity"], audit["gate_loosening"]["loosened"]), ("stale", True), audit)
            self.assertEqual(audit["gate_loosening"]["loosened_visual_hooks"], ["pixels"])


class TestFixRound5WrappersBooleanFlagsSegmentsAndHarnessCovers(unittest.TestCase):
    """Command-head-only unwrapping, execution wrappers, boolean regeneration flags, per-command threshold
    alignment, and formal invariants covering `.factonio/` files."""

    _visual_repo = staticmethod(TestFixRound2VisualGuardAndHarnessGaps._visual_repo)
    _set_command = staticmethod(TestFixRound2VisualGuardAndHarnessGaps._set_command)
    _assert_cases = TestFixRound4CompoundThresholdsAndShortFlags._assert_cases

    def test_env_and_shells_unwrap_only_at_command_head(self):
        pw, base = "npx playwright test", "true"
        self._assert_cases({
            "path ending in env": (base, f"{pw} tests/env --update-snapshots --threshold 0.99", True),
            "jest path ending in env -u": (base, "npx jest tests/env -u", True),
            "grep env -u": (base, "npx jest --grep env -u", True),
            "positional bash -u": (base, "npx jest bash -u", True),
            "positional sh -c": (base, "npx jest sh -c x -u", True),
            "attached env -S": (base, "env -S'npx jest -u'", True),
            "env -S spaced": (base, "env -S 'npx jest -u'", True),
            "env --split-string=": (base, "env --split-string='npx jest -u'", True),
            "env -S appends trailing args": (base, "env -S'npx jest' -u", True),
            "env -u NAME": (base, "env -u CI npx jest", False),
            "env after &&": (base, "true && env -u CI npx jest", False),
            "VAR=val env -u": (base, "CI=1 env -u HOME python3 -u check.py", False),
        })

    def test_execution_wrappers_resolve_interpreter_flags(self):
        self._assert_cases({
            "timeout 30 python3 -u": ("true", "timeout 30 python3 -u visual_check.py", False),
            "timeout -k 5 30 python3 -u": ("true", "timeout -k 5 30 python3 -u visual_check.py", False),
            "timeout --signal=TERM": ("true", "timeout --signal=TERM 30 python3 -u visual_check.py", False),
            "timeout -s KILL": ("true", "timeout -s KILL 30 python3 -u visual_check.py", False),
            "uv run python3 -u": ("true", "uv run python3 -u script.py", False),
            "uv run --with pillow": ("true", "uv run --with pillow python3 -u script.py", False),
            "uv run --python 3.12 --": ("true", "uv run --python 3.12 -- python3 -u script.py", False),
            "nice -n 5": ("true", "nice -n 5 python3 -u script.py", False),
            "nohup": ("true", "nohup python3 -u script.py", False),
            "time -p": ("true", "time -p python3 -u script.py", False),
            "stdbuf -oL": ("true", "stdbuf -oL python3 -u script.py", False),
            "stdbuf -o L": ("true", "stdbuf -o L python3 -u script.py", False),
            "command -p": ("true", "command -p python3 -u script.py", False),
            "exec -a": ("true", "exec -a ui python3 -u script.py", False),
            "argv timeout": ("true", ["timeout", "30", "python3", "-u", "visual_check.py"], False),
            "timeout sh -c": ("true", "timeout 30 sh -c 'python3 -u visual_check.py'", False),
            "timeout bash -u script": ("true", "timeout 30 bash -u check.sh", False),
            "timeout 30 npx jest -u": ("true", "timeout 30 npx jest -u", True),
            "uv run pytest --snapshot-update": ("true", "uv run pytest --snapshot-update", True),
            "uv run python3 -m pytest": ("true", "uv run python3 -m pytest --snapshot-update", True),
            "uv run -m pytest": ("true", "uv run -m pytest --snapshot-update", True),
            "nice timeout jest -u": ("true", "nice -n 5 timeout 30 npx jest -u", True),
            "timeout python script -u": ("true", "timeout 30 python3 check.py -u", True),
        })

    def test_boolean_regeneration_flags_ignore_trailing_words(self):
        pw = "npx playwright test"
        self._assert_cases({
            'jest -u ""': ("true", 'npx jest -u ""', True),
            'vitest -u ""': ("true", 'npx vitest -u ""', True),
            "jest -u none": ("true", "npx jest -u none", True),
            "jest -u false": ("true", "npx jest -u false", True),
            "--updateSnapshot false": ("true", "npx jest --updateSnapshot false", True),
            'pytest --snapshot-update ""': ("true", 'python3 -m pytest --snapshot-update ""', True),
            "--accept no": ("true", "cargo insta test --accept no", True),
            '--update-snapshots ""': ("true", f'{pw} --update-snapshots ""', True),
            "--update-snapshots=": ("true", f"{pw} --update-snapshots=", True),
            "--update-snapshots none": ("true", f"{pw} --update-snapshots none", False),
            "--update-snapshots=none": ("true", f"{pw} --update-snapshots=none", False),
            "--updateSnapshot=false": ("true", "npx jest --updateSnapshot=false", False),
            "UPDATE_SNAPSHOTS= empty": ("true", f"UPDATE_SNAPSHOTS= {pw}", False),
        })

    def test_thresholds_align_per_command_signature(self):
        a, b, c = "python3 check_snap.py a", "python3 check_snap.py b", "python3 check_snap.py c"
        mixed = f"{a} --threshold 0.01 && {b} --threshold 0.05"
        single = f"{a} --threshold 0.01"
        self._assert_cases({
            "swap + dummy trailing": (mixed, f"{a} --threshold 0.05 && {b} --threshold 0.01 && true --threshold 0.01",
                                      True),
            "raise below later + dummy": (mixed, f"{a} --threshold 0.04 && {b} --threshold 0.05 && "
                                          "true --threshold 0.01", True),
            "new check appended": (mixed, f"{mixed} && {c} --threshold 0.02", False),
            "new check prepended": (mixed, f"{c} --threshold 0.02 && {mixed}", False),
            "new check between": (mixed, f"{a} --threshold 0.01 && {c} --threshold 0.02 && {b} --threshold 0.05",
                                  False),
            "single raised + dummy": (single, f"{a} --threshold 0.50 && true --threshold 0.01", True),
            "renamed raised + dummy": (single, "other --threshold 0.50 && true --threshold 0.01", True),
            "renamed same": (single, "other --threshold 0.01", False),
            "tightened later": (mixed, f"{a} --threshold 0.01 && {b} --threshold 0.02", False),
            "command removed": (mixed, single, True),
            "extra same-flag occurrence raised": (single, f"{single} --threshold 0.5", True),
            "threshold added to existing command": (f"{a}", f"{a} --threshold 0.5", True),
            "new check nan": (mixed, f"{mixed} && {c} --threshold nan", True),
        })

    def test_wrapped_interpreter_flags_pass_check_harness_end_to_end(self):
        from factonio.harness import audit_harness, check_harness

        with isolated_hermes_home() as home:
            repo = self._visual_repo(home, "python3 visual_check.py", name="svc")
            _write(repo, "visual_check.py", "print('ok')\n")
            _commit_all(repo, "visual check script")
            for command in ("timeout 30 python3 -u visual_check.py", "timeout -k 5 30 python3 -u visual_check.py",
                            "uv run --no-project python3 -u visual_check.py"):
                if command.startswith("uv ") and not shutil.which("uv"):
                    continue  # the parser cases above cover `uv run` without the binary
                with self.subTest(command):
                    self._set_command(repo, command)
                    chk = check_harness(str(repo), run_hooks=True)
                    self.assertEqual(chk["hook_results"]["failed_required"], [], chk["hook_results"])
                    self.assertTrue(chk["valid"], chk["errors"])
                    self.assertFalse(chk["gate_loosening"]["loosened"], chk["gate_loosening"])
                    audit = audit_harness(str(repo))
                    self.assertFalse(audit["gate_loosening"]["loosened"], audit["gate_loosening"])
            self._set_command(repo, "timeout 30 npx jest -u")
            chk = check_harness(str(repo))
            self.assertFalse(chk["valid"])
            self.assertEqual(chk["gate_loosening"]["loosened_visual_hooks"], ["pixels"])

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


class TestFixRound6ChangedCommandThresholdsFallbacksPathsAndRedirects(unittest.TestCase):
    """New thresholds on edited commands, `||` fallbacks, repo-local wrapper-named paths, redirections and
    bundled `env -iS`."""

    _visual_repo = staticmethod(TestFixRound2VisualGuardAndHarnessGaps._visual_repo)
    _set_command = staticmethod(TestFixRound2VisualGuardAndHarnessGaps._set_command)
    _assert_cases = TestFixRound4CompoundThresholdsAndShortFlags._assert_cases

    def _reasons(self, base: Any, live: Any) -> str:
        from factonio.harness import detect_harness_gate_loosening

        with isolated_hermes_home() as home:
            repo = self._visual_repo(home, base, name="svc")
            self._set_command(repo, live)
            return json.dumps(detect_harness_gate_loosening(repo)["findings"])

    def test_new_threshold_on_edited_command_is_loosening(self):
        a, b, c = "python3 check_snap.py a", "python3 check_snap.py b", "python3 check_snap.py c"
        mixed = f"{a} --threshold 0.01 && {b} --threshold 0.05"
        pw = "npx playwright test"
        self._assert_cases({
            "--verbose --threshold": ("python3 check.py", "python3 check.py --verbose --threshold 0.5", True),
            "--reporter --max-diff-pixels": (pw, f"{pw} --reporter=line --max-diff-pixels=99999", True),
            "CI=1 --max-diff-pixels": (pw, f"CI=1 {pw} --max-diff-pixels=99999", True),
            "python3 -u --threshold": ("python3 check_ui.py", "python3 -u check_ui.py --threshold 0.99", True),
            "redirect + threshold": ("python3 check.py", "python3 check.py --threshold 0.5 2>&1", True),
            "verbose only": ("python3 check.py", "python3 check.py --verbose", False),
            "zero threshold on edited": ("python3 check.py", "python3 check.py --verbose --threshold 0", False),
            "new check appended": (mixed, f"{mixed} && {c} --threshold 0.02", False),
        })

    def test_or_fallback_is_loosening(self):
        self._assert_cases({
            "|| second run": ("npx jest --threshold 0.01", "npx jest --threshold 0.01 || npx jest --threshold 0.05",
                              True),
            "|| true": ("a --threshold 0.01", "a --threshold 0.01 || true", True),
            "sh -c || true": ("a --threshold 0.01", "sh -c 'a --threshold 0.01 || true'", True),
            "|| unchanged": ("a --threshold 0.01 || b", "a --threshold 0.01 || b", False),
            "&& added": ("a --threshold 0.01", "a --threshold 0.01 && c --threshold 0.02", False),
        })
        self.assertIn("adds shell `||` fallback", self._reasons("a --threshold 0.01", "a --threshold 0.01 || true"))

    def test_relative_paths_named_like_wrappers_are_not_unwrapped(self):
        cases: dict[str, tuple[Any, Any, bool]] = {}
        for path in ("tests/env", "./bin/env", "scripts/timeout", "scripts/nice", "scripts/time",
                     "scripts/python3"):
            cases[path] = (f"{path} --threshold 0.01", f"{path} --update-snapshots --threshold 0.99", True)
        for path in ("scripts/sh", "scripts/bash"):
            cases[path] = (f"{path} --threshold 0.01", f"{path} --update-snapshots --threshold 0.01", True)
        cases["scripts/sh -u"] = ("scripts/sh --threshold 0.01", "scripts/sh -u --threshold 0.01", True)
        cases["tests/env bare"] = ("tests/env", "tests/env --update-snapshots", True)
        cases["true -> tests/env"] = ("true --threshold 0.01", "tests/env --update-snapshots --threshold 0.99", True)
        cases["absolute /usr/bin/env -u NAME"] = ("true", "/usr/bin/env -u CI python3 -u check.py", False)
        cases["absolute /bin/sh -c"] = ("true", "/bin/sh -c 'python3 -u check.py'", False)
        self._assert_cases(cases)
        for path in ("tests/env", "./bin/env", "scripts/timeout", "scripts/python3"):
            with self.subTest(f"{path} reasons"):
                reasons = self._reasons(f"{path} --threshold 0.01", f"{path} --update-snapshots --threshold 0.99")
                self.assertIn("`--update-snapshots`", reasons)
                self.assertIn("loosens threshold `threshold`", reasons)

    def test_system_wrappers_never_swallow_gate_flags(self):
        self._assert_cases({
            "env --update-snapshots": ("true", "env --update-snapshots npx playwright test", True),
            "timeout --update-snapshots": ("true", "timeout --update-snapshots 30 npx playwright test", True),
            "nice --threshold": ("nice npx jest --threshold 0.01", "nice --threshold 0.99 npx jest", True),
            "uv run --update-snapshots": ("true", "uv run --update-snapshots pytest", True),
            "python3 --update-snapshots": ("true", "python3 --update-snapshots check.py", True),
            "sh --update-snapshots -c": ("true", "sh --update-snapshots -c 'python3 check.py'", True),
            "bash --update-snapshots script": ("true", "bash --update-snapshots check.sh", True),
        })

    def test_redirections_stay_in_the_same_simple_command(self):
        self._assert_cases({
            "leading > file": ("true", "> output.txt python3 -u check.py", False),
            "leading 2>/dev/null": ("true", "2>/dev/null python3 -u check.py", False),
            "trailing > file 2>&1": ("true", "python3 -u check.py > output.txt 2>&1", False),
            "&>> log": ("true", "python3 -u check.py &>> log.txt", False),
            "< input": ("true", "< in.txt python3 -u check.py", False),
            "leading > file jest -u": ("true", "> output.txt npx jest -u", True),
            "trailing redirect jest -u": ("true", "npx jest -u 2>/dev/null", True),
        })

    def test_env_bundled_split_string_and_scoped_recursion(self):
        five = " && ".join(["env -S 'true'"] * 4 + ["env -S 'npx jest -u'"])
        self._assert_cases({
            "env -iS": ("true", 'env -iS "npx jest -u"', True),
            "env -iS attached": ("true", "env -iS'npx jest -u'", True),
            "env -iu NAME": ("true", "env -iu CI python3 -u check.py", False),
            "five sequential env -S": ("true", five, True),
            "env -S scoped to its command": ("true", "env -S 'python3' -u check.py && npx playwright test", False),
        })

    def test_redirected_python_and_edited_threshold_end_to_end(self):
        from factonio.harness import audit_harness, check_harness

        script = ("import sys\n"
                  "t = float(sys.argv[sys.argv.index('--threshold') + 1]) if '--threshold' in sys.argv else 0.01\n"
                  "sys.exit(1 if 0.25 > t else 0)\n")
        with isolated_hermes_home() as home:
            repo = self._visual_repo(home, "python3 check.py", name="svc")
            _write(repo, "check.py", script)
            _commit_all(repo, "visual check script")
            self.assertEqual(check_harness(str(repo), run_hooks=True)["hook_results"]["failed_required"], ["pixels"])
            self._set_command(repo, "python3 check.py --verbose --threshold 0.5")
            chk = check_harness(str(repo), run_hooks=True)
            self.assertEqual(chk["hook_results"]["failed_required"], [])
            self.assertFalse(chk["valid"])
            self.assertTrue(chk["gate_loosening"]["loosened"])
            audit = audit_harness(str(repo))
            self.assertEqual((audit["severity"], audit["gate_loosening"]["loosened"]), ("stale", True))

            self._set_command(repo, "> output.txt python3 -u check.py 2>&1")
            chk = check_harness(str(repo))
            self.assertFalse(chk["gate_loosening"]["loosened"], chk["gate_loosening"])



class TestFixRound7TargetAwareThresholdsUnitsEnvAndShellArgs(unittest.TestCase):
    """Thresholds never transfer between edited/reordered targets, `%` keeps its unit, environment assignments are
    never exempt as new checks, and `sh -c SCRIPT $0 ARGS` / `bash --rcfile FILE -c` are parsed POSIX-correctly."""

    _visual_repo = staticmethod(TestFixRound2VisualGuardAndHarnessGaps._visual_repo)
    _set_command = staticmethod(TestFixRound2VisualGuardAndHarnessGaps._set_command)
    _assert_cases = TestFixRound4CompoundThresholdsAndShortFlags._assert_cases

    _PIXELS = ("import sys\n"
               "diff = {'a': 0.25, 'b': 0.0}[sys.argv[1]]\n"
               "sys.exit(1 if diff > float(sys.argv[sys.argv.index('--threshold') + 1]) else 0)\n")
    _PERCENT = ("import sys\n"
                "raw = sys.argv[sys.argv.index('--threshold') + 1]\n"
                "t = float(raw[:-1]) / 100 if raw.endswith('%') else float(raw)\n"
                "sys.exit(1 if 0.25 > t else 0)\n")
    _ENV = "import os, sys\nsys.exit(1 if 0.25 > float(os.environ.get('THRESHOLD', '0.01')) else 0)\n"
    _REGEN = "import sys\nsys.exit(0 if '-u' in sys.argv else 1)\n"

    def _assert_end_to_end(self, script: str, base: Any, loosening: list[Any], clean: list[Any], *,
                           base_fails: bool = True) -> None:
        from factonio.harness import audit_harness, check_harness

        with isolated_hermes_home() as home:
            repo = self._visual_repo(home, base, name="svc")
            for name in ("pixels.py", "check.py"):
                _write(repo, name, script)
            _commit_all(repo, "visual check script")
            committed = check_harness(str(repo), run_hooks=True)
            self.assertEqual(committed["hook_results"]["failed_required"], ["pixels"] if base_fails else [])
            for live in loosening:
                with self.subTest(live=live):
                    self._set_command(repo, live)
                    chk = check_harness(str(repo), run_hooks=True)
                    self.assertEqual(chk["hook_results"]["failed_required"], [], chk["hook_results"])
                    self.assertFalse(chk["valid"])
                    self.assertTrue(chk["gate_loosening"]["loosened"], chk["gate_loosening"])
                    audit = audit_harness(str(repo))
                    self.assertEqual((audit["severity"], audit["gate_loosening"]["loosened"]), ("stale", True))
            for live in clean:
                with self.subTest(clean=live):
                    self._set_command(repo, live)
                    chk = check_harness(str(repo))
                    self.assertFalse(chk["gate_loosening"]["loosened"], chk["gate_loosening"])

    def test_thresholds_keep_target_identity_across_edited_reordered_commands(self):
        a, b = "python3 pixels.py a", "python3 pixels.py b"
        base = f"{a} --threshold 0.01 && {b} --threshold 0.9"
        self._assert_cases({
            "edited + reordered budget swap": (base, f"{b} --verbose --threshold 0.01 && {a} --verbose --threshold 0.5",
                                               True),
            "exact reorder": (base, f"{b} --threshold 0.9 && {a} --threshold 0.01", False),
            "edited + reordered tightening": (base, f"{b} --verbose --threshold 0.5 && {a} --verbose "
                                                    "--threshold 0.005", False),
            "single edited command tightened": ("python3 check.py --threshold 0.05",
                                                "python3 check.py --verbose --threshold 0.01", False),
            "ambiguous renamed targets": (base, "x --threshold 0.01 && y --threshold 0.5", True),
            "ambiguous renamed targets same strictest": (base, "x --threshold 0.01 && y --threshold 0.01", False),
            "target removed among edits": (base, f"{b} --verbose --threshold 0.9", True),
        })
        self._assert_end_to_end(self._PIXELS, base,
                                [f"{b} --verbose --threshold 0.01 && {a} --verbose --threshold 0.5"],
                                [f"{b} --threshold 0.9 && {a} --threshold 0.01"])

    def test_percentage_thresholds_keep_their_unit(self):
        cmd = "python3 check.py --threshold"
        cases = {
            "1% -> 0.5": ("1%", "0.5", True),
            "0.5 -> 10%": ("0.5", "10%", False),
            "1% -> 5%": ("1%", "5%", True),
            "10% -> 5%": ("10%", "5%", False),
            "0.01 -> 2%": ("0.01", "2%", True),
            "1% -> 0.005": ("1%", "0.005", False),
            "1% -> 5 ambiguous unit": ("1%", "5", True),
            "5 -> 1% ambiguous unit": ("5", "1%", True),
            "5% unchanged": ("5%", "5%", False),
        }
        self._assert_cases({k: (f"{cmd} {b}", f"{cmd} {l}", x) for k, (b, l, x) in cases.items()})
        from factonio.harness import _visual_hook_loosening

        unit = _visual_hook_loosening({"command": f"{cmd} 1%"}, {"command": f"{cmd} 5"})
        self.assertTrue(any("changes threshold `threshold` unit from 1% to 5" in r for r in unit), unit)
        self._assert_end_to_end(self._PERCENT, f"{cmd} 1%", [f"{cmd} 0.5", f"{cmd} 5"], [f"{cmd} 0.005"])
        self._assert_end_to_end(self._PERCENT, f"{cmd} 0.5", [f"{cmd} 60%"], [f"{cmd} 30%", f"{cmd} 0.5"],
                                base_fails=False)

    def test_environment_assignments_are_never_exempt_as_new_checks(self):
        check = "python3 check.py"
        loosening = [f"export THRESHOLD=0.5; {check}", f"export THRESHOLD=0.5 && {check}",
                     f"THRESHOLD=0.5; export THRESHOLD; {check}", f"declare -x THRESHOLD=0.5; {check}"]
        cases = {live: (check, live, True) for live in loosening}
        cases.update({
            "typeset MAX_DIFF": (check, f"typeset -x MAX_DIFF=3; {check}", True),
            "export beside command threshold": (f"{check} --threshold 0.01",
                                                f"export THRESHOLD=0.5; {check} --threshold 0.01", True),
            "env assignment raised": (f"THRESHOLD=0.01; {check}", f"export THRESHOLD=0.5; {check}", True),
            "env assignment kept": (f"THRESHOLD=0.01; {check}", f"export THRESHOLD=0.01; {check}", False),
            "export zero": (check, f"export THRESHOLD=0; {check}", False),
            "export unrelated": (check, f"export CI=1; {check}", False),
            "unchanged": (check, check, False),
        })
        self._assert_cases(cases)
        # `declare` is a bash builtin; hooks run under /bin/sh (dash on Debian/Ubuntu), so only POSIX forms execute.
        self._assert_end_to_end(self._ENV, check, loosening[:3], [f"export THRESHOLD=0; {check}", check])

    def test_sh_c_positional_args_and_shell_value_options(self):
        script = 'python3 check.py "$@"'
        loosening: list[Any] = [
            f"sh -c '{script}' sh -u", f"bash -c '{script}' bash -u", ["sh", "-c", script, "sh", "-u"],
            f"sh -c '{script}' python3 -u", f"sh -c '{script}' diff -u", f"sh -c '{script}' env -u -u",
            "bash --rcfile /dev/null -c 'python3 check.py -u'", "bash --init-file /dev/null -c 'python3 check.py -u'",
        ]
        cases: dict[str, tuple[Any, Any, bool]] = {json.dumps(live): ("python3 check.py", live, True)
                                                   for live in loosening}
        cases.update({
            "sh -c $0 only": ("python3 check.py", f"sh -c '{script}' sh", False),
            "rcfile python3 -u": ("python3 check.py", "bash --rcfile /dev/null -c 'python3 -u check.py'", False),
            "sh -c then &&": ("python3 check.py", "sh -c 'python3 check.py' && true", False),
        })
        self._assert_cases(cases)
        self._assert_end_to_end(self._REGEN, "python3 check.py",
                                [f"sh -c '{script}' sh -u", "bash --rcfile /dev/null -c 'python3 check.py -u'"],
                                [f"sh -c '{script}' sh"])


class TestFixRound8GroupedFlagsEnvDashShellClustersAndEnvOrder(unittest.TestCase):
    """Bundled `-vu`/`-uv` update options, POSIX `env -`, `-oc`/`-oo`/`-Oc` shell clusters, and environment
    thresholds that never borrow another command's budget or lose their assignment order."""

    _visual_repo = staticmethod(TestFixRound2VisualGuardAndHarnessGaps._visual_repo)
    _set_command = staticmethod(TestFixRound2VisualGuardAndHarnessGaps._set_command)
    _assert_cases = TestFixRound4CompoundThresholdsAndShortFlags._assert_cases
    _assert_end_to_end = TestFixRound7TargetAwareThresholdsUnitsEnvAndShellArgs._assert_end_to_end

    _ARGPARSE = ("import argparse, sys\n"
                 "p = argparse.ArgumentParser()\n"
                 "p.add_argument('-u', action='store_true')\n"
                 "p.add_argument('-v', action='store_true')\n"
                 "p.add_argument('--threshold', type=float, default=0.01)\n"
                 "a = p.parse_args()\n"
                 "sys.exit(0 if a.u or a.threshold >= 0.25 else 1)\n")
    _TARGETS = ("import os, sys\n"
                "args = sys.argv[1:]\n"
                "t = float(args[args.index('--threshold') + 1]) if '--threshold' in args "
                "else float(os.environ.get('THRESHOLD', '0.01'))\n"
                "sys.exit(1 if 0.25 > t else 0)\n")

    def test_grouped_short_update_options_are_regeneration(self):
        from factonio.harness import _command_tokens, _regen_flags

        for cmd in ("python3 check.py -vu", "python3 check.py -uv", "npx jest -qu", "npm test -- -vu"):
            with self.subTest(cmd):
                self.assertTrue(_regen_flags(_command_tokens(cmd)), cmd)
        for cmd in ("python3 -u check.py", "python3 -Bu check.py", "diff -ru a b", "sh -eu check.sh",
                    "set -eu; python3 check.py", "python3 check.py -v", "env -u HOME python3 check.py"):
            with self.subTest(clean=cmd):
                self.assertEqual(_regen_flags(_command_tokens(cmd)), set(), cmd)
        clean = ["python3 -u check.py", "python3 -Bu check.py", "diff -ru a b", "sh -eu check.sh",
                 "set -eu; python3 check.py"]
        cases = {live: ("python3 check.py", live, True) for live in ("python3 check.py -vu", "python3 check.py -uv")}
        cases.update({c: ("python3 check.py", c, False) for c in clean})
        self._assert_cases(cases)
        self._assert_end_to_end(self._ARGPARSE, "python3 check.py",
                                ["python3 check.py -vu", "python3 check.py -uv"], clean)

    def test_posix_env_dash_is_an_env_option(self):
        loosening = ["env - sh -c 'python3 check.py -u'", "env - sh -c 'python3 check.py --threshold 0.5'"]
        cases = {live: ("python3 check.py", live, True) for live in loosening}
        cases["env - python3 -u"] = ("python3 check.py", "env - python3 -u check.py", False)
        self._assert_cases(cases)
        self._assert_end_to_end(self._ARGPARSE, "python3 check.py", loosening, ["env - python3 -u check.py"])
        from factonio.harness import check_harness

        with isolated_hermes_home() as home:
            repo = self._visual_repo(home, "python3 check.py")
            _write(repo, "check.py", "import sys\nsys.exit(0)\n")
            _commit_all(repo, "passing visual check")
            self._set_command(repo, "env - python3 -u check.py")
            chk = check_harness(str(repo), run_hooks=True)
            self.assertEqual(chk["hook_results"]["failed_required"], [], chk["hook_results"])
            self.assertTrue(chk["valid"], chk)
            self.assertFalse(chk["gate_loosening"]["loosened"], chk["gate_loosening"])

    def test_shell_option_clusters_consume_one_word_per_o(self):
        loosening = ["bash -oc pipefail 'python3 check.py -u'", "sh -oc errexit 'python3 check.py --threshold 0.5'",
                     "bash -oo pipefail errexit -c 'python3 check.py -u'", "bash -Oc extglob 'python3 check.py -u'"]
        cases = {live: ("python3 check.py", live, True) for live in loosening}
        cases["bash -ou errexit -u script.sh"] = ("python3 check.py", "bash -ou errexit -u script.sh", False)
        self._assert_cases(cases)
        self._assert_end_to_end(self._ARGPARSE, "python3 check.py", loosening, ["bash -ou errexit -u script.sh"])

    def test_environment_thresholds_respect_default_commands_and_assignment_order(self):
        a, b = "python3 check.py a", "python3 check.py b"
        base = f"{a} --threshold 0.9 && {b}"
        borrow = [f"export THRESHOLD=0.9; {base}", f"sh -c '{a} --threshold 0.9 && {b} \"$@\"' sh --threshold 0.9"]
        cases = {live: (base, live, True) for live in borrow}
        cases.update({
            "assignment order swapped": ("THRESHOLD=0.9; THRESHOLD=0.01; python3 check.py",
                                         "THRESHOLD=0.01; THRESHOLD=0.9; python3 check.py", True),
            "assignment order kept": ("THRESHOLD=0.9; THRESHOLD=0.01; python3 check.py",
                                      "THRESHOLD=0.9; THRESHOLD=0.01; python3 check.py", False),
            "every command already at the bar": (f"{a} --threshold 0.9 && {b} --threshold 0.9",
                                                 f"export THRESHOLD=0.9; {a} --threshold 0.9 && {b} --threshold 0.9",
                                                 False),
        })
        self._assert_cases(cases)
        self._assert_end_to_end(self._TARGETS, base, borrow, [base])
        exported = "export THRESHOLD=0.9; export THRESHOLD=0.01; python3 check.py a"
        self._assert_end_to_end(self._TARGETS, exported,
                                ["export THRESHOLD=0.01; export THRESHOLD=0.9; python3 check.py a"], [exported])


class TestFixRound9LiveFrozenShellArgsWordFlagsCommentsAndOverrides(unittest.TestCase):
    """Live-declared frozen baselines, field-split `sh -c` positional args, `sh -c -`, single-dash word options,
    leading `VAR=val` before `export`, trailing threshold overrides and POSIX `#` comments."""

    _visual_repo = staticmethod(TestFixRound2VisualGuardAndHarnessGaps._visual_repo)
    _set_command = staticmethod(TestFixRound2VisualGuardAndHarnessGaps._set_command)
    _assert_cases = TestFixRound4CompoundThresholdsAndShortFlags._assert_cases
    _assert_end_to_end = TestFixRound7TargetAwareThresholdsUnitsEnvAndShellArgs._assert_end_to_end

    # last `--threshold` wins (argparse), `$THRESHOLD` is the default, unknown words such as `||` are ignored
    _CHECK = ("import argparse, os, sys\n"
              "p = argparse.ArgumentParser()\n"
              "p.add_argument('-u', action='store_true')\n"
              "p.add_argument('-v', action='store_true')\n"
              "p.add_argument('--threshold', type=float, default=float(os.environ.get('THRESHOLD', '0.01')))\n"
              "a, _ = p.parse_known_args()\n"
              "sys.exit(0 if a.u or a.threshold >= 0.25 else 1)\n")

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

    def test_sh_c_positional_args_are_field_split(self):
        loosening = ["sh -c '$0' 'python3 check.py -u'", "sh -c '$1' sh 'python3 check.py -u'",
                     "sh -c '$0' 'python3 check.py --threshold 0.99'", "bash -c '$0' 'python3 check.py -u'",
                     "sh -c 'python3 check.py \"$@\"' sh -u"]
        cases = {live: ("python3 check.py", live, True) for live in loosening}
        cases.update({
            "$0 || fallback": ("python3 check.py", "sh -c '$0' 'python3 check.py || true'", True),
            "nested sh -c in $0": ("python3 check.py", "sh -c '$0' 'sh -c \"python3 check.py -u\"'", True),
            "$0 plain": ("python3 check.py", "sh -c '$0' 'python3 check.py'", False),
            "$0 = sh": ("python3 check.py", "sh -c 'python3 check.py \"$@\"' sh", False),
        })
        self._assert_cases(cases)
        self._assert_end_to_end(self._CHECK, "python3 check.py", loosening, ["sh -c '$0' 'python3 check.py'"])

    def test_lone_dash_ends_shell_options(self):
        loosening = ["sh -c - 'python3 check.py -u'", "sh -c - 'python3 check.py --threshold 0.99'",
                     "sh -c - 'python3 check.py || true'", "bash -c - 'python3 check.py -u'"]
        cases = {live: ("python3 check.py", live, True) for live in loosening}
        cases["sh -c - plain"] = ("python3 check.py", "sh -c - 'python3 check.py'", False)
        cases["sh - script args"] = ("python3 check.py", "sh - check.sh -v", False)
        self._assert_cases(cases)
        self._assert_end_to_end(self._CHECK, "python3 check.py", loosening, ["sh -c - 'python3 check.py'"])

    def test_single_dash_word_options_are_not_bundled_update_flags(self):
        from factonio.harness import _command_tokens, _regen_flags

        for cmd in ("compare -metric AE -fuzz 0% a.png b.png d.png", "magick compare -fuzz 5% a.png b.png d.png",
                    "go test -run TestVisual -count 1 -timeout 60s -cpu 2 ./...", "find . -user root",
                    "pytest -quiet -suite visual -junit out.xml -summary -debug -full -auto",
                    "node check.js -out o -output o -url u -num 3 -sub s -module m -include i -exclude e"):
            with self.subTest(clean=cmd):
                self.assertEqual(_regen_flags(_command_tokens(cmd)), set(), cmd)
        for cmd in ("python3 check.py -vu", "python3 check.py -uv", "npx jest -qu", "npx jest -su", "npx jest -xu",
                    "npx jest -vsu", "npx jest -u", "go test -update ./...", "node check.js -update"):
            with self.subTest(cmd):
                self.assertTrue(_regen_flags(_command_tokens(cmd)), cmd)
        self._assert_cases({
            "compare -fuzz 0%": ("compare -metric AE a.png b.png d.png",
                                 "compare -metric AE -fuzz 0% a.png b.png d.png", False),
            "go test word flags": ("go test ./...", "go test -run TestVisual -count 1 -timeout 60s -cpu 2 ./...",
                                   False),
            "go test -update": ("go test ./...", "go test -update ./...", True),
            "-vu": ("python3 check.py", "python3 check.py -vu", True),
            "-uv": ("python3 check.py", "python3 check.py -uv", True),
        })

    def test_leading_assignments_before_export_still_feed_every_command(self):
        check = "python3 check.py"
        loosening = [f"CI=1 export THRESHOLD=0.5; {check}", f"CI=1 THRESHOLD=0.5; export THRESHOLD; {check}"]
        cases = {live: (check, live, True) for live in loosening}
        cases["CI=1 export CI"] = (check, f"CI=1 export CI; {check}", False)
        self._assert_cases(cases)
        self._assert_end_to_end(self._CHECK, check, loosening, [f"CI=1 export CI; {check}"])

    def test_trailing_threshold_override_is_capped_by_the_strictest_base_value(self):
        exported = "export THRESHOLD=0.9; export THRESHOLD=0.01; python3 check.py"
        cli = "python3 check.py --threshold 0.9 --threshold 0.01"
        env_live = "export THRESHOLD=0.9; export THRESHOLD=0.01; export THRESHOLD=0.9; python3 check.py"
        self._assert_cases({
            "env override": (exported, env_live, True),
            "cli override": (cli, cli + " --threshold 0.9", True),
            "cli strict extra": (cli, cli + " --threshold 0.005", False),
        })
        self._assert_end_to_end(self._CHECK, exported, [env_live], [exported])
        self._assert_end_to_end(self._CHECK, cli, [cli + " --threshold 0.9"], [cli, cli + " --threshold 0.005"])

    def test_posix_comments_are_not_live_tokens(self):
        from factonio.harness import _split_shell

        self.assertEqual(_split_shell("true # --threshold 0.01\npython3 check.py"),
                         ["true", "\n", "python3", "check.py"])
        self.assertEqual(_split_shell("a --color=#fff foo#bar '#q' \"#d\" \\#e"),
                         ["a", "--color=#fff", "foo#bar", "#q", "#d", "#e"])
        base = "python3 check.py --threshold 0.01"
        loosening = [f"# {base}", "true # --threshold 0.01"]
        clean = ["python3 check.py --label foo#bar --threshold 0.01",
                 'python3 check.py --label "#notacomment" --threshold 0.01']
        cases = {live: (base, live, True) for live in loosening}
        cases.update({live: (base, live, False) for live in clean})
        self._assert_cases(cases)
        self._assert_end_to_end(self._CHECK, base, loosening, clean)



class TestFixRound10FirstHarnessFrozenBaselinesEvalAndEnvThresholdClears(unittest.TestCase):
    """Frozen baselines of a first, not-yet-committed harness, command-head POSIX `eval`, and committed threshold
    environment variables cleared by `unset`, `export -n`, `env -u`, `env -i` or `env -` before the check."""

    _visual_repo = staticmethod(TestFixRound2VisualGuardAndHarnessGaps._visual_repo)
    _set_command = staticmethod(TestFixRound2VisualGuardAndHarnessGaps._set_command)
    _assert_cases = TestFixRound4CompoundThresholdsAndShortFlags._assert_cases
    _assert_end_to_end = TestFixRound7TargetAwareThresholdsUnitsEnvAndShellArgs._assert_end_to_end
    _CHECK = TestFixRound9LiveFrozenShellArgsWordFlagsCommentsAndOverrides._CHECK
    # an unset THRESHOLD falls back to a lenient tool default, so clearing it turns the failing diff green
    _ENV = ("import os, sys\n"
            "t = os.environ.get('THRESHOLD')\n"
            "sys.exit(1 if 0.25 > float(t if t is not None else '1.0') else 0)\n")

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

    def test_command_head_eval_is_unwrapped(self):
        from factonio.harness import _command_tokens, _regen_flags

        self.assertEqual(_regen_flags(_command_tokens("npx jest eval -u")), {"-u"})
        self.assertEqual(_regen_flags(_command_tokens("npx jest eval")), set())
        self.assertEqual(_regen_flags(_command_tokens("eval 'python3 check.py -u'")), {"-u"})
        loosening = ["eval 'python3 check.py -u'", "eval 'python3 check.py --threshold 0.5'",
                     "eval 'python3 check.py || true'", "eval -- 'python3 check.py -u'",
                     "eval python3 check.py -u", "sh -c \"eval 'python3 check.py -u'\""]
        cases = {live: ("python3 check.py", live, True) for live in loosening}
        cases.update({
            "eval at the matching threshold": ("python3 check.py --threshold 0.01",
                                               "eval 'python3 check.py --threshold 0.01'", False),
            "eval plain": ("python3 check.py", "eval 'python3 check.py'", False),
        })
        self._assert_cases(cases)
        # dash's `eval` runs `--` as a command, so `eval --` only executes as written under bash-like /bin/sh.
        executed = [live for live in loosening if not live.startswith("eval --")]
        self._assert_end_to_end(self._CHECK, "python3 check.py", executed, ["eval 'python3 check.py'"])
        self._assert_end_to_end(self._CHECK, "python3 check.py --threshold 0.01", [],
                                ["eval 'python3 check.py --threshold 0.01'"])

    _CLEARS = ("unset THRESHOLD; {c}", "unset -v THRESHOLD; {c}", "unset -- THRESHOLD; {c}",
               "bash -c 'export -n THRESHOLD; {c}'", "env -u THRESHOLD {c}", "env --unset=THRESHOLD {c}",
               "env --unset THRESHOLD {c}", "env -iu THRESHOLD PATH=\"$PATH\" {c}", "env -i PATH=\"$PATH\" {c}",
               "env --ignore-environment PATH=\"$PATH\" {c}", "env - PATH=\"$PATH\" {c}")

    def test_clearing_an_exported_threshold_variable_is_loosening(self):
        check = "python3 check.py"
        base = f"export THRESHOLD=0.01; {check}"
        loosening = [f"export THRESHOLD=0.01; {form.format(c=check)}" for form in self._CLEARS]
        loosening += [f"export THRESHOLD=0.01; sh -c 'unset THRESHOLD; {check}'",
                      f"export THRESHOLD=0.01; eval 'unset THRESHOLD'; {check}",
                      f"export THRESHOLD=0.01; unset THRESHOLD; THRESHOLD=0.01; {check}",
                      f"export THRESHOLD=0.01; unset THRESHOLD; export THRESHOLD=0.5; {check}",
                      f"export THRESHOLD=0.01; env -i PATH=\"$PATH\" THRESHOLD=0.5 {check}"]
        clean = [f"export THRESHOLD=0.01; unset FOO; {check}", f"export THRESHOLD=0.01; env -u FOO {check}",
                 f"export -n FOO; export THRESHOLD=0.01; {check}",
                 f"export THRESHOLD=0.01; env -i PATH=\"$PATH\" THRESHOLD=0.01 {check}",
                 f"export THRESHOLD=0.01; unset THRESHOLD; export THRESHOLD=0.01; {check}",
                 f"export THRESHOLD=0.01; unset -f THRESHOLD; {check}", f"{base}; unset THRESHOLD", base]
        cases = {live: (base, live, True) for live in loosening}
        cases.update({live: (base, live, False) for live in clean})
        cases.update({
            "declare +x": (base, f"export THRESHOLD=0.01; declare +x THRESHOLD; {check}", True),
            "no committed env threshold: env -": (check, "env - python3 -u check.py", False),
            "no committed env threshold: unset": (check, f"unset THRESHOLD; {check}", False),
            "committed env -i kept": (f"export THRESHOLD=0.01; env -i PATH=\"$PATH\" {check}",
                                      f"export THRESHOLD=0.01; env -i PATH=\"$PATH\" {check}", False),
        })
        self._assert_cases(cases)
        self._assert_end_to_end(self._ENV, base, loosening[:len(self._CLEARS)], clean)
        self._assert_reset_still_enforced(base, f"export THRESHOLD=0.01; env -i PATH=\"$PATH\" THRESHOLD=0.01 {check}")

    def test_clearing_a_prefix_threshold_assignment_is_loosening(self):
        check = "python3 check.py"
        base = f"THRESHOLD=0.01 {check}"
        loosening = [f"THRESHOLD=0.01 sh -c 'unset THRESHOLD; {check}'",
                     f"THRESHOLD=0.01 sh -c 'unset -v THRESHOLD; {check}'",
                     f"THRESHOLD=0.01 bash -c 'export -n THRESHOLD; {check}'",
                     *(f"THRESHOLD=0.01 {form.format(c=check)}" for form in self._CLEARS if form.startswith("env"))]
        clean = [f"THRESHOLD=0.01 env -u FOO {check}", f"unset FOO; {base}", f"export -n FOO; {base}",
                 f"THRESHOLD=0.01 env -i PATH=\"$PATH\" THRESHOLD=0.01 {check}",
                 f"unset THRESHOLD; {base}", base]
        cases = {live: (base, live, True) for live in loosening}
        cases.update({live: (base, live, False) for live in clean})
        self._assert_cases(cases)
        self._assert_end_to_end(self._ENV, base, loosening, clean)
        self._assert_reset_still_enforced(base, f"THRESHOLD=0.01 env -i PATH=\"$PATH\" THRESHOLD=0.01 {check}")

    def _assert_reset_still_enforced(self, base: str, live: str) -> None:
        from factonio.harness import check_harness

        with isolated_hermes_home() as home:
            repo = self._visual_repo(home, base)
            _write(repo, "check.py", self._ENV)
            _commit_all(repo, "visual check script")
            self._set_command(repo, live)
            chk = check_harness(str(repo), run_hooks=True)
            self.assertEqual(chk["hook_results"]["failed_required"], ["pixels"], chk["hook_results"])
            self.assertFalse(chk["gate_loosening"]["loosened"], chk["gate_loosening"])


class TestFixRound11FrozenContractExportScopeEnvSplitUnsetAndBuiltin(unittest.TestCase):
    """The frozen-baseline contract fixed before Doctor/hooks run, thresholds that actually reach the check's
    environment (exported, in scope, before it), `env -S` words re-parsed as env options, `unset --` and bash's
    command-head `builtin`."""

    _visual_repo = staticmethod(TestFixRound2VisualGuardAndHarnessGaps._visual_repo)
    _set_command = staticmethod(TestFixRound2VisualGuardAndHarnessGaps._set_command)
    _assert_cases = TestFixRound4CompoundThresholdsAndShortFlags._assert_cases
    _assert_end_to_end = TestFixRound7TargetAwareThresholdsUnitsEnvAndShellArgs._assert_end_to_end
    _assert_reset_still_enforced = TestFixRound10FirstHarnessFrozenBaselinesEvalAndEnvThresholdClears._assert_reset_still_enforced
    _uncommitted_harness = staticmethod(TestFixRound10FirstHarnessFrozenBaselinesEvalAndEnvThresholdClears._uncommitted_harness)
    _CHECK = TestFixRound9LiveFrozenShellArgsWordFlagsCommentsAndOverrides._CHECK
    _ENV = TestFixRound10FirstHarnessFrozenBaselinesEvalAndEnvThresholdClears._ENV
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

    _EXPORTED = "export THRESHOLD=0.01; python3 check.py"
    _UNEXPORTED = ("THRESHOLD=0.01; python3 check.py", "python3 check.py; export THRESHOLD=0.01",
                   "(export THRESHOLD=0.01); python3 check.py", "sh -c 'export THRESHOLD=0.01'; python3 check.py",
                   "export THRESHOLD=0.01 | true; python3 check.py", "declare THRESHOLD=0.01; python3 check.py",
                   "readonly THRESHOLD=0.01; python3 check.py", "THRESHOLD=0.01; set -a; python3 check.py")

    def test_threshold_must_still_be_exported_before_the_check(self):
        from factonio.harness import _visual_hook_loosening

        base = self._EXPORTED
        cases = {live: (base, live, True) for live in self._UNEXPORTED}
        cases.update({
            "export kept": (base, base, False),
            "export tightened": (base, "export THRESHOLD=0.005; python3 check.py", False),
            "assign then export": (base, "THRESHOLD=0.01; export THRESHOLD; python3 check.py", False),
            "export name first": (base, "export THRESHOLD; THRESHOLD=0.01; python3 check.py", False),
            "declare -x": (base, "declare -x THRESHOLD=0.01; python3 check.py", False),
            "set -a": (base, "set -a; THRESHOLD=0.01; python3 check.py", False),
            "same subshell": (base, "(export THRESHOLD=0.01; python3 check.py)", False),
            "same sh -c": (base, "sh -c 'export THRESHOLD=0.01; python3 check.py'", False),
            "exported into sh -c": (base, "export THRESHOLD=0.01; sh -c 'python3 check.py'", False),
            "loosened in subshell": (base, "(export THRESHOLD=0.9; python3 check.py)", True),
            "prefix unchanged": ("THRESHOLD=0.01 python3 check.py", "THRESHOLD=0.01 python3 check.py", False),
            "bare assign + export loosens": ("python3 check.py", "THRESHOLD=0.5; export THRESHOLD; python3 check.py",
                                             True),
            "unexported base kept": ("THRESHOLD=0.01; python3 check.py", "THRESHOLD=0.01; python3 check.py", False),
        })
        self._assert_cases(cases)
        reasons = _visual_hook_loosening({"command": base}, {"command": "python3 check.py; export THRESHOLD=0.01"})
        self.assertTrue(any("no longer exports threshold `threshold=0.01`" in r for r in reasons), reasons)
        # `declare`/`readonly` and `set -a` are exercised by the parser cases; dash runs these POSIX forms
        executed = [live for live in self._UNEXPORTED if not live.startswith(("declare", "readonly"))]
        self._assert_end_to_end(self._ENV, base, executed, [
            base, "export THRESHOLD=0.005; python3 check.py", "THRESHOLD=0.01; export THRESHOLD; python3 check.py",
            "(export THRESHOLD=0.01; python3 check.py)", "set -a; THRESHOLD=0.01; python3 check.py",
        ])
        for live in ("THRESHOLD=0.01; export THRESHOLD; python3 check.py", "set -a; THRESHOLD=0.01; python3 check.py",
                     "export THRESHOLD=0.01; sh -c 'python3 check.py'"):
            with self.subTest(still_enforced=live):
                self._assert_reset_still_enforced(base, live)

    _ENV_SPLIT_CLEARS = ("env -S '-u THRESHOLD python3 check.py'", "env -S '-i python3 check.py'",
                         "env -S'-i python3 check.py'", "env -S '- python3 check.py'",
                         "env -S '--unset=THRESHOLD python3 check.py'", "env --split-string='-i python3 check.py'",
                         "env --split-string '-u THRESHOLD python3 check.py'")

    def test_env_split_string_words_are_parsed_as_env_options(self):
        from factonio.harness import _command_tokens, _regen_flags

        base = self._EXPORTED
        cases = {live: (base, f"export THRESHOLD=0.01; {live}", True) for live in self._ENV_SPLIT_CLEARS}
        cases.update({
            "-i before -S": (base, "export THRESHOLD=0.01; env -i -S 'PATH=/usr/bin:/bin python3 check.py'", True),
            "-u before -S": (base, "export THRESHOLD=0.01; env -u THRESHOLD -S 'python3 check.py'", True),
            "-iS bundle": (base, "export THRESHOLD=0.01; env -iS 'python3 check.py'", True),
            "nested -S": (base, "export THRESHOLD=0.01; env -S \"-S '-u THRESHOLD python3 check.py'\"", True),
            "-i re-assigns": (base, "export THRESHOLD=0.01; env -S '-i THRESHOLD=0.01 python3 check.py'", False),
            "-i before -S re-assigns": (base, "export THRESHOLD=0.01; env -i -S 'THRESHOLD=0.01 python3 check.py'",
                                        False),
            "-u FOO": (base, "export THRESHOLD=0.01; env -S '-u FOO python3 check.py'", False),
            "unbuffered -u FOO": ("python3 check.py", "env -S '-u FOO python3 -u check.py'", False),
            "unbuffered -i PATH": ("python3 check.py", "env -S '-i PATH=/usr/bin:/bin python3 -u check.py'", False),
            "regen still caught": ("python3 check.py", "env -S '-u FOO python3 check.py -u'", True),
        })
        self._assert_cases(cases)
        self.assertEqual(_regen_flags(_command_tokens("env -S '-u FOO python3 -u check.py'")), set())
        self.assertEqual(_regen_flags(_command_tokens("env -S '-i PATH=/usr/bin:/bin npx jest -u'")), {"-u"})
        # `-i`/`-` keep PATH for the executed hooks so `python3` resolves on any runner
        executed = [live.replace("-i ", '-i PATH=$PATH ').replace("'- ", "'- PATH=$PATH ").replace("'", '"')
                    for live in self._ENV_SPLIT_CLEARS]
        self._assert_end_to_end(self._ENV, base, [f"export THRESHOLD=0.01; {live}" for live in executed], [])
        self._assert_reset_still_enforced(base, 'export THRESHOLD=0.01; env -S "-i PATH=$PATH THRESHOLD=0.01 '
                                                'python3 check.py"')
        from factonio.harness import check_harness

        for live in ("env -S '-u FOO python3 -u check.py'", 'env -S "-i PATH=$PATH python3 -u check.py"'):
            with self.subTest(valid=live), isolated_hermes_home() as home:
                repo = self._visual_repo(home, "python3 check.py")
                _write(repo, "check.py", self._ENV)  # THRESHOLD unset: the committed check passes
                _commit_all(repo, "visual check script")
                self._set_command(repo, live)
                chk = check_harness(str(repo), run_hooks=True)
                self.assertTrue(chk["valid"], chk["errors"])
                self.assertFalse(chk["gate_loosening"]["loosened"], chk["gate_loosening"])

    def test_unset_double_dash_makes_every_word_an_operand(self):
        base = self._EXPORTED
        self._assert_cases({
            "unset -- -f": (base, "export THRESHOLD=0.01; unset -- -f THRESHOLD; python3 check.py", True),
            "bash unset -- -f": (base, "export THRESHOLD=0.01; bash -c 'unset -- -f THRESHOLD; python3 check.py'",
                                 True),
            "unset -f": (base, "export THRESHOLD=0.01; unset -f THRESHOLD; python3 check.py", False),
            "unset -f --": (base, "export THRESHOLD=0.01; unset -f -- THRESHOLD; python3 check.py", False),
        })
        # dash aborts on `unset -- -f` (bad variable name); bash unsets THRESHOLD and runs the check
        self._assert_end_to_end(self._ENV, base,
                                ["export THRESHOLD=0.01; bash -c 'unset -- -f THRESHOLD; python3 check.py'"],
                                ["export THRESHOLD=0.01; bash -c 'unset -f THRESHOLD; python3 check.py'"])

    _BUILTIN_REGEN = ("bash -c 'builtin eval \"python3 check.py -u\"'", "builtin eval 'python3 check.py -u'")
    _BUILTIN_CLEARS = ("export THRESHOLD=0.01; bash -c 'builtin unset THRESHOLD; python3 check.py'",
                       "export THRESHOLD=0.01; builtin export -n THRESHOLD; python3 check.py",
                       "export THRESHOLD=0.01; bash -c 'builtin declare +x THRESHOLD; python3 check.py'")

    def test_command_head_builtin_is_unwrapped_like_command(self):
        from factonio.harness import _command_tokens, _regen_flags

        self.assertEqual(_regen_flags(_command_tokens("builtin eval 'python3 check.py -u'")), {"-u"})
        self.assertEqual(_regen_flags(_command_tokens("npx jest builtin -u")), {"-u"})
        self.assertEqual(_regen_flags(_command_tokens("python3 check.py builtin")), set())
        cases = {live: ("python3 check.py", live, True) for live in self._BUILTIN_REGEN}
        cases.update({live: (self._EXPORTED, live, True) for live in self._BUILTIN_CLEARS})
        cases.update({
            "builtin -- eval": ("python3 check.py", "builtin -- eval 'python3 check.py -u'", True),
            "builtin unset FOO": (self._EXPORTED, "export THRESHOLD=0.01; bash -c 'builtin unset FOO; "
                                                  "python3 check.py'", False),
            "builtin export kept": (self._EXPORTED, "builtin export THRESHOLD=0.01; python3 check.py", False),
            "builtin eval plain": ("python3 check.py", "bash -c 'builtin eval \"python3 check.py\"'", False),
        })
        self._assert_cases(cases)
        # `builtin` is a bash builtin (dash has none): execute through `bash -c`
        self._assert_end_to_end(self._CHECK, "python3 check.py", [self._BUILTIN_REGEN[0]],
                                ["bash -c 'builtin eval \"python3 check.py\"'"])
        self._assert_end_to_end(self._ENV, self._EXPORTED, [
            self._BUILTIN_CLEARS[0], self._BUILTIN_CLEARS[2],
            "export THRESHOLD=0.01; bash -c 'builtin export -n THRESHOLD; python3 check.py'",
        ], ["export THRESHOLD=0.01; bash -c 'builtin unset FOO; python3 check.py'"])


if __name__ == "__main__":
    unittest.main()
