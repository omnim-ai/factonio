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
                    self.assertIn(audit_harness(str(repo))["severity"], ("clean", "stale"))
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
        shutil.which("node") and shutil.which("uvx") and shutil.which("git"),
        "running the harness hooks needs git, node and uvx on PATH (provided in CI)",
    )
    def test_check_with_doctor_and_hooks_and_read_only_clean_audit(self):
        from factonio.harness import audit_harness, check_harness

        with isolated_hermes_home() as home:
            repo = _copy_tracked_checkout(PLUGIN_ROOT, home / "dogfood" / "factonio")
            # Doctor/hooks call `python3`; resolve it to this interpreter, which has hermes_cli and PyYAML.
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
                        "new_cli_commands", "removed_cli_commands"):
                self.assertEqual(audit[key], [], key)


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


if __name__ == "__main__":
    unittest.main()
