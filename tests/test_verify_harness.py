"""Clean-room tests for the repo-local verification harness (`harness.py`) and its wiring."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shlex
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
