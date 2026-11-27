"""Clean-room tests for the opt-in rigor="high" multi-vendor verification panel (Wave 3 Unit 3)."""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest import mock

from tests._env import PLUGIN_ROOT, isolated_hermes_home

if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

TWO_VENDORS = ["anthropic/claude-opus-4-6", "openai/gpt-5"]
ROUTED_SAME_VENDOR = ["openrouter/anthropic/claude-opus-4-6", "anthropic/claude-sonnet-4-6"]


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


def _register(home: Path, spec: str = "acme/svc", *, git_repo: bool = False) -> Path:
    from factonio.repos import add_repo

    if git_repo:
        from tests.test_verify_harness import _make_repo

        local = _make_repo(home / "dev", spec.split("/")[1])
    else:
        local = home / "dev" / spec.split("/")[1]
        (local / ".git").mkdir(parents=True, exist_ok=True)
    add_repo(spec, local_path=str(local), clone=False, inspect_remote=False)
    return local


def _set_repo_field(slug: str, **fields: Any) -> None:
    from factonio.config import list_repos, save_repos_store

    repos = list_repos()
    repos[slug].update(fields)
    save_repos_store(repos)


def _set_aux_panel(home: Path, panel: Any) -> None:
    import hermes_yaml as yaml

    cfg = home / "config.yaml"
    data = yaml.safe_load(cfg.read_text(encoding="utf-8")) if cfg.is_file() else {}
    data = data or {}
    data.setdefault("auxiliary", {})["factonio_verify_panel"] = panel
    cfg.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _get_task(task_id: str):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing

    with connect_closing() as conn:
        task = kb.get_task(conn, task_id)
    assert task is not None
    return task


def _complete(task_id: str, verdict: Optional[str], **meta: Any) -> None:
    """Complete a card; ``verdict=None`` omits ``metadata.verdict`` entirely."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing

    metadata = dict(meta) if verdict is None else {"verdict": verdict, **meta}
    with connect_closing() as conn:
        ok = kb.complete_task(conn, task_id, summary=f"verdict {verdict}", metadata=metadata)
    assert ok, task_id


def _task_count() -> int:
    from hermes_cli.kanban_db_connect import connect_closing

    with connect_closing() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])


def _trail_rows(decision: str, tenant: Optional[str] = None) -> list[dict[str, Any]]:
    from factonio.trail import query_decisions

    return [r for r in query_decisions(tenant=tenant, limit=500) if r["decision"] == decision]


def _lane(status: str, verdict: Optional[str], vendor: Optional[str], phase: str = "verify-diff", **extra: Any) -> dict:
    return {"status": status, "verdict": verdict, "vendor": vendor, "phase": phase, **extra}


class TestRigorResolution(unittest.TestCase):
    def test_precedence_and_validation(self):
        from factonio.config import RIGOR_LEVELS, resolve_rigor, save_settings

        self.assertEqual(RIGOR_LEVELS, ("standard", "high"))
        with isolated_hermes_home() as home:
            _register(home)
            self.assertEqual(resolve_rigor(), "standard")
            self.assertEqual(resolve_rigor(repo_slug="acme-svc"), "standard")

            save_settings({"default_rigor": "high"})
            self.assertEqual(resolve_rigor(repo_slug="acme-svc"), "high")

            _set_repo_field("acme-svc", default_rigor="standard")
            self.assertEqual(resolve_rigor(repo_slug="acme/svc"), "standard")
            self.assertEqual(resolve_rigor(), "high")

            self.assertEqual(resolve_rigor(repo_slug="acme-svc", override="HIGH"), "high")
            self.assertEqual(resolve_rigor(repo_slug="acme-svc", override="inherit"), "standard")
            with self.assertRaisesRegex(ValueError, "rigor must be one of standard, high"):
                resolve_rigor(override="paranoid")

            _set_repo_field("acme-svc", default_rigor="extreme")
            with self.assertRaisesRegex(ValueError, "default_rigor"):
                resolve_rigor(repo_slug="acme-svc")

    def test_defaults_declared_in_settings_and_manifest(self):
        import hermes_yaml as yaml

        from factonio.config import DEFAULT_SETTINGS

        self.assertEqual(DEFAULT_SETTINGS["default_rigor"], "standard")
        self.assertEqual(DEFAULT_SETTINGS["verify_panel"], [])
        schema = yaml.safe_load((PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8"))["config_schema"]
        self.assertEqual(schema["default_rigor"]["default"], "standard")
        self.assertEqual(schema["default_rigor"]["choices"], ["standard", "high"])
        self.assertEqual(schema["verify_panel"]["default"], [])


class TestPanelValidation(unittest.TestCase):
    def test_normalize_entry_shapes(self):
        from factonio.config import _normalize_panel_entry

        e = _normalize_panel_entry("openrouter/anthropic/claude-opus-4-6", source="s", index=0)
        assert e is not None
        self.assertEqual(
            (e["provider"], e["model"], e["vendor"], e["label"]),
            ("openrouter", "anthropic/claude-opus-4-6", "anthropic", "openrouter/anthropic/claude-opus-4-6"),
        )
        e = _normalize_panel_entry({"provider": "vertex", "model": "gemini-2.5-pro", "label": "Gemini"}, source="s", index=1)
        assert e is not None
        self.assertEqual((e["provider"], e["vendor"], e["label"], e["index"]), ("vertex", "google", "Gemini", 1))
        e = _normalize_panel_entry("bedrock/us.anthropic.claude-sonnet-4", source="s", index=2)
        assert e is not None
        self.assertEqual(e["vendor"], "anthropic")
        e = _normalize_panel_entry("gpt-5", source="s", index=3)
        assert e is not None
        self.assertEqual((e["provider"], e["model"], e["vendor"]), (None, "gpt-5", "openai"))
        e = _normalize_panel_entry({"model": "gpt-5", "label": " GPT (v5)\r\n\talt\x00 "}, source="s", index=4)
        assert e is not None
        self.assertEqual(e["label"], "GPT (v5) alt")
        for bad in (None, 7, {"provider": "openai"}, "", "  "):
            with self.subTest(bad=bad):
                self.assertIsNone(_normalize_panel_entry(bad, source="s", index=0))

    def test_router_backed_same_vendor_collapses(self):
        from factonio.config import validate_verify_panel

        res = validate_verify_panel(ROUTED_SAME_VENDOR)
        self.assertFalse(res["valid"])
        self.assertEqual(res["vendors"], ["anthropic"])
        self.assertEqual(res["distinct_vendor_count"], 1)
        self.assertEqual(res["duplicate_vendors"], ["anthropic"])
        self.assertIn("single vendor family (anthropic)", res["warning"])

    def test_diverse_router_panel_is_valid(self):
        from factonio.config import validate_verify_panel

        res = validate_verify_panel(
            ["bedrock/us.anthropic.claude-sonnet-4", "vertex/gemini-2.5-pro", {"provider": "openai", "model": "gpt-5"}]
        )
        self.assertTrue(res["valid"], res)
        self.assertEqual(res["vendors"], ["anthropic", "google", "openai"])
        self.assertIsNone(res["warning"])
        self.assertEqual(set(res), {
            "entries", "vendors", "distinct_vendor_count", "duplicate_vendors", "unknown_vendor_models",
            "valid", "warning", "source", "advisories",
        })

    def test_unknown_vendor_and_too_few_models_fail(self):
        from factonio.config import validate_verify_panel

        res = validate_verify_panel(["anthropic/claude-opus-4-6", "custom/mystery-model"])
        self.assertFalse(res["valid"])
        self.assertEqual(res["unknown_vendor_models"], ["custom/mystery-model"])
        self.assertIn("could not be inferred", res["warning"])

        res = validate_verify_panel(["custom/foo", "custom/bar"])
        self.assertFalse(res["valid"])
        self.assertEqual(res["vendors"], [])

        res = validate_verify_panel(["openai/gpt-5"])
        self.assertFalse(res["valid"])
        self.assertIn("at least 2", res["warning"])

        res = validate_verify_panel([])
        self.assertFalse(res["valid"])
        self.assertIn("no verify_panel is configured", res["warning"])

        res = validate_verify_panel(["anthropic/claude-opus-4-6", "openai/gpt-5", "custom/mystery"])
        self.assertTrue(res["valid"])
        self.assertEqual(res["unknown_vendor_models"], ["custom/mystery"])
        self.assertTrue(any("mystery" in a for a in res["advisories"]))

    def test_resolution_precedence(self):
        from factonio.config import resolve_verify_panel, save_settings

        with isolated_hermes_home() as home:
            _register(home)
            res = resolve_verify_panel(repo_slug="acme-svc")
            self.assertEqual((res["source"], res["valid"]), ("unset", False))

            save_settings({"verify_panel": ["anthropic/claude-opus-4-6", "google/gemini-2.5-pro"]})
            res = resolve_verify_panel(repo_slug="acme-svc")
            self.assertEqual((res["source"], res["vendors"]), ("settings.verify_panel", ["anthropic", "google"]))

            _set_aux_panel(home, {"models": ["openai/gpt-5", "xai/grok-4"]})
            res = resolve_verify_panel(repo_slug="acme-svc")
            self.assertEqual((res["source"], res["vendors"]), ("auxiliary.factonio_verify_panel", ["openai", "xai"]))

            _set_repo_field("acme-svc", models={"verify_panel": ROUTED_SAME_VENDOR})
            res = resolve_verify_panel(repo_slug="acme-svc")
            self.assertEqual(res["source"], "repo:acme-svc.models.verify_panel")
            self.assertFalse(res["valid"])

            res = resolve_verify_panel(repo_slug="acme-svc", override=["deepseek/deepseek-v3", "qwen/qwen3-coder"])
            self.assertEqual((res["source"], res["vendors"], res["valid"]), ("override", ["deepseek", "qwen"], True))
            self.assertEqual(resolve_verify_panel(override=[])["source"], "auxiliary.factonio_verify_panel")


class TestPerRepoDefaultRigor(unittest.TestCase):
    def test_update_repo_sets_clears_and_validates(self):
        from factonio.config import resolve_rigor
        from factonio.repos import EDITABLE_REPO_FIELDS, update_repo

        self.assertIn("default_rigor", EDITABLE_REPO_FIELDS)
        with isolated_hermes_home() as home:
            _register(home)
            store = home / "plugin-data" / "factonio" / "repos.json"
            res = update_repo("acme/svc", default_rigor="High")
            self.assertEqual(res["updated_fields"], ["default_rigor"])
            self.assertEqual(json.loads(store.read_text())["acme-svc"]["default_rigor"], "high")
            self.assertEqual(resolve_rigor(repo_slug="acme-svc"), "high")

            before = store.read_text()
            with self.assertRaisesRegex(ValueError, "default_rigor must be one of standard, high"):
                update_repo("acme/svc", default_rigor="max", merge_policy="ask")
            self.assertEqual(store.read_text(), before)

            for clear in ("inherit", None, "", "default"):
                update_repo("acme/svc", default_rigor="high")
                update_repo("acme/svc", default_rigor=clear)
                with self.subTest(clear=clear):
                    self.assertNotIn("default_rigor", json.loads(store.read_text())["acme-svc"])

    def test_tool_and_cli(self):
        with isolated_hermes_home() as home:
            _register(home)
            store = home / "plugin-data" / "factonio" / "repos.json"
            handler = _tools()["factonio_update_repo"]["handler"]
            res = json.loads(handler({"repo": "acme/svc", "default_rigor": "high"}))
            self.assertTrue(res["ok"], res)
            self.assertEqual(json.loads(store.read_text())["acme-svc"]["default_rigor"], "high")
            res = json.loads(handler({"repo": "acme/svc", "default_rigor": "ultra"}))
            self.assertFalse(res["ok"])

            run = _cli()
            code, out, _ = run("update", "acme-svc", "--default-rigor", "inherit", "--json")
            self.assertEqual(code, 0, out)
            self.assertEqual(json.loads(out)["updated_fields"], ["default_rigor"])
            self.assertNotIn("default_rigor", json.loads(store.read_text())["acme-svc"])
            code, out, _ = run("update", "acme-svc", "--default-rigor", "high")
            self.assertEqual(code, 0)
            self.assertIn("Default rigor: high", out)
            code, out, _ = run("list")
            self.assertIn("rigor=high", out)
            code, _, _ = run("update", "acme-svc", "--default-rigor", "bogus")
            self.assertNotEqual(code, 0)

    def test_models_cli_and_doctor_surface_panel(self):
        from factonio.config import save_settings
        from factonio.profile_setup import run_doctor

        with isolated_hermes_home() as home:
            _register(home)
            run = _cli()
            code, out, _ = run("--json", "models")
            self.assertEqual(code, 0)
            body = json.loads(out)
            self.assertEqual(body["default_rigor"], "standard")
            self.assertFalse(body["verify_panel"]["valid"])
            self.assertIn("models", body)

            doc = run_doctor(PLUGIN_ROOT)
            self.assertEqual(doc["default_rigor"], "standard")
            self.assertFalse(any("rigor=high" in w for w in doc["warnings"]))

            save_settings({"default_rigor": "high", "verify_panel": ROUTED_SAME_VENDOR})
            doc = run_doctor(PLUGIN_ROOT)
            self.assertTrue(any("fail closed" in w and "anthropic" in w for w in doc["warnings"]), doc["warnings"])
            code, out, _ = run("models")
            self.assertIn("default rigor -> high", out)
            self.assertIn("INVALID", out)
            self.assertIn("Warning: rigor=high verify panel collapses", out)

            save_settings({"default_rigor": "standard", "verify_panel": TWO_VENDORS})
            _set_repo_field("acme-svc", default_rigor="high", models={"verify_panel": ["openai/gpt-5"]})
            doc = run_doctor(PLUGIN_ROOT)
            self.assertTrue(any(w.startswith("Repository acme-svc has default_rigor=high") for w in doc["warnings"]))
            self.assertTrue(doc["verify_panel"]["valid"])

    def test_dashboard_api(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from factonio.config import save_settings

        with isolated_hermes_home() as home:
            _register(home)
            save_settings({"verify_panel": TWO_VENDORS})
            spec = importlib.util.spec_from_file_location(
                "factonio_test_plugin_api_panel", PLUGIN_ROOT / "dashboard" / "plugin_api.py"
            )
            assert spec is not None and spec.loader is not None
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            app = FastAPI()
            app.include_router(mod.router, prefix="/api/plugins/factonio")
            client = TestClient(app)
            base = "/api/plugins/factonio"
            no_claude = {"claude_bin": None, "has_credentials": False, "available": False}
            with mock.patch.object(mod._config, "detect_claude_design_support", return_value=no_claude):
                body = client.get(f"{base}/repos").json()
                self.assertEqual(body["defaults"]["default_rigor"], "standard")
                item = body["repos"][0]
                self.assertIsNone(item["default_rigor"])
                self.assertEqual(item["effective_default_rigor"], "standard")

                r = client.patch(f"{base}/repos/acme-svc", json={"default_rigor": "high"})
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(r.json()["repo"]["default_rigor"], "high")
                self.assertEqual(r.json()["repo"]["effective_default_rigor"], "high")
                r = client.patch(f"{base}/repos/acme-svc", json={"default_rigor": "max"})
                self.assertEqual(r.status_code, 422)
                r = client.patch(f"{base}/repos/acme-svc", json={"default_rigor": "inherit"})
                self.assertIsNone(r.json()["repo"]["default_rigor"])

                body = client.get(f"{base}/models", params={"repo": "acme-svc"}).json()
                self.assertIn("models", body)
                self.assertIn("cross_model_warning", body)
                self.assertEqual(body["default_rigor"], "standard")
                self.assertTrue(body["verify_panel"]["valid"])
                self.assertEqual(body["verify_panel"]["vendors"], ["anthropic", "openai"])


class TestPanelSpawn(unittest.TestCase):
    STANDARD_KEYS = {
        "ok", "tenant", "repo", "pr_number", "verify_diff_task_id", "verify_blackbox_task_id", "diff_model",
        "blackbox_model", "subscribed", "harness_present", "harness_path",
        "target_mode", "branch", "base_branch", "head_sha",
    }

    def test_standard_rigor_unchanged(self):
        from factonio.orchestrator import holdout_dir_path, spawn_verification_pair

        with isolated_hermes_home() as home:
            _register(home)
            res = spawn_verification_pair("acme/svc", "t-std", 5, session_id="s")
            self.assertEqual(set(res), self.STANDARD_KEYS | {"rigor", "panel"})
            self.assertEqual((res["rigor"], res["panel"]), ("standard", None))
            self.assertEqual(_task_count(), 2)
            diff, bb = _get_task(res["verify_diff_task_id"]), _get_task(res["verify_blackbox_task_id"])
            self.assertEqual(diff.title, "[verify:diff] acme/svc#5")
            self.assertEqual(bb.title, "[verify:blackbox] acme/svc#5")
            for body in (diff.body, bb.body):
                self.assertNotIn("Verification panel", body)
            self.assertNotIn(str(holdout_dir_path("t-std")), diff.body)
            self.assertIn(str(holdout_dir_path("t-std")), bb.body)
            row = _trail_rows("spawn_verify", "t-std")[0]
            self.assertEqual(row["evidence"]["rigor"], "standard")
            self.assertNotIn("panel_id", row["evidence"])

    def test_high_rigor_fails_closed(self):
        from factonio.config import save_settings
        from factonio.orchestrator import spawn_verification_pair

        with isolated_hermes_home() as home:
            _register(home)
            with self.assertRaisesRegex(ValueError, "no verify_panel is configured"):
                spawn_verification_pair("acme/svc", "t-fc", 1, rigor="high")
            with self.assertRaisesRegex(ValueError, "single vendor family"):
                spawn_verification_pair("acme/svc", "t-fc", 1, rigor="high", panel_models=ROUTED_SAME_VENDOR)
            with self.assertRaisesRegex(ValueError, "at least 2"):
                spawn_verification_pair("acme/svc", "t-fc", 1, rigor="high", panel_models=["openai/gpt-5"])
            with self.assertRaisesRegex(ValueError, "could not be inferred"):
                spawn_verification_pair(
                    "acme/svc", "t-fc", 1, rigor="high", panel_models=["openai/gpt-5", "custom/mystery"]
                )
            _set_repo_field("acme-svc", default_rigor="high")
            save_settings({"verify_panel": ["openai/gpt-5"]})
            with self.assertRaises(ValueError):
                spawn_verification_pair("acme/svc", "t-fc", 1)
            with self.assertRaisesRegex(ValueError, "rigor must be one of"):
                spawn_verification_pair("acme/svc", "t-fc", 1, rigor="extreme")
            self.assertEqual(_task_count(), 0)
            self.assertEqual(_trail_rows("spawn_verify"), [])
            # An explicit standard override still works on a high-rigor repo.
            res = spawn_verification_pair("acme/svc", "t-fc", 1, rigor="standard")
            self.assertEqual((res["rigor"], _task_count()), ("standard", 2))

    def test_high_rigor_fan_out_preserves_barriers_and_harness(self):
        from factonio.config import save_settings
        from factonio.harness import bootstrap_verify_harness
        from factonio.orchestrator import holdout_dir_path, spawn_verification_pair

        with isolated_hermes_home() as home:
            repo = _register(home, git_repo=True)
            bootstrap_verify_harness("acme/svc")
            _set_repo_field("acme-svc", default_rigor="high")
            save_settings({"verify_panel": [
                "openrouter/anthropic/claude-opus-4-6",
                {"provider": "openai", "model": "gpt-5", "label": "GPT-5"},
            ]})
            res = spawn_verification_pair("acme/svc", "t-hi", 7, head_sha="a" * 40, session_id="s")
            self.assertEqual(set(res), self.STANDARD_KEYS | {"rigor", "panel"})
            self.assertEqual(res["rigor"], "high")
            self.assertTrue(res["harness_present"])
            panel = res["panel"]
            self.assertRegex(panel["panel_id"], r"^vp_[0-9a-f]{10}$")
            self.assertEqual(panel["vendors"], ["anthropic", "openai"])
            self.assertEqual(panel["source"], "settings.verify_panel")
            self.assertEqual(len(panel["lanes"]), 2)
            self.assertEqual(res["verify_diff_task_id"], panel["lanes"][0]["verify_diff_task_id"])
            self.assertEqual(res["verify_blackbox_task_id"], panel["lanes"][0]["verify_blackbox_task_id"])
            self.assertEqual(_task_count(), 4)

            holdout = str(holdout_dir_path("t-hi"))
            harness_md = str(repo / ".factonio" / "verify.md")
            expected = {
                "anthropic": ("openrouter", "anthropic/claude-opus-4-6", "openrouter/anthropic/claude-opus-4-6"),
                "openai": ("openai", "gpt-5", "GPT-5"),
            }
            for lane in panel["lanes"]:
                vendor = lane["vendor"]
                provider, model, label = expected[vendor]
                stamp = f"- Verification panel: `{panel['panel_id']}` (rigor=`high`, vendor=`{vendor}`, model=`{label}`)"
                diff, bb = _get_task(lane["verify_diff_task_id"]), _get_task(lane["verify_blackbox_task_id"])
                with self.subTest(vendor=vendor):
                    self.assertEqual(diff.title, f"[verify:diff] acme/svc#7 [panel:{vendor}]")
                    self.assertEqual(bb.title, f"[verify:blackbox] acme/svc#7 [panel:{vendor}]")
                    for task in (diff, bb):
                        self.assertIn(stamp, task.body)
                        self.assertEqual((task.provider_override, task.model_override), (provider, model))
                        self.assertIn("a" * 40, task.body)
                    self.assertEqual(diff.skills, ["factonio:verify-diff"])
                    self.assertEqual(bb.skills, ["factonio:verify-blackbox"])
                    self.assertNotIn(holdout, diff.body)
                    self.assertNotIn(".factonio", diff.body)
                    self.assertIn("gh pr diff 7", diff.body)
                    self.assertIn(holdout, bb.body)
                    self.assertIn("## Repo Verification Harness", bb.body)
                    self.assertIn(harness_md, bb.body)
                    self.assertIn("NEVER run `git diff`", bb.body)
                    self.assertNotIn("gh pr diff", bb.body)

            row = _trail_rows("spawn_verify", "t-hi")[0]
            ev = row["evidence"]
            self.assertEqual((ev["rigor"], ev["panel_id"], ev["vendors"]), ("high", panel["panel_id"], ["anthropic", "openai"]))
            self.assertEqual([lane["vendor"] for lane in ev["panel_lanes"]], ["anthropic", "openai"])
            self.assertTrue(ev["harness_present"])

    def test_spawn_verify_tool_passes_rigor_and_panel(self):
        with isolated_hermes_home() as home:
            _register(home)
            tools = _tools()
            props = tools["factonio_spawn_verify"]["schema"]["parameters"]["properties"]
            self.assertEqual(props["rigor"]["enum"], ["standard", "high"])
            self.assertIn("panel_models", props)
            handler = tools["factonio_spawn_verify"]["handler"]
            bad = json.loads(handler({
                "repo": "acme/svc", "tenant": "t-tool", "pr_number": 3, "rigor": "high",
                "panel_models": ROUTED_SAME_VENDOR,
            }))
            self.assertFalse(bad["ok"])
            self.assertIn("single vendor family", bad["error"])
            res = json.loads(handler({
                "repo": "acme/svc", "tenant": "t-tool", "pr_number": 3, "rigor": "high",
                "panel_models": ["anthropic/claude-opus-4-6", {"provider": "google", "model": "gemini-2.5-pro"}],
            }))
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["panel"]["vendors"], ["anthropic", "google"])
            self.assertEqual(res["panel"]["source"], "override")
            std = json.loads(handler({"repo": "acme/svc", "tenant": "t-tool", "pr_number": 4}))
            self.assertEqual((std["rigor"], std["panel"]), ("standard", None))


class TestAggregatePanelVerdicts(unittest.TestCase):
    def setUp(self):
        from factonio.orchestrator import aggregate_panel_verdicts

        self.agg = aggregate_panel_verdicts

    def test_unanimous_pass(self):
        res = self.agg([
            _lane("done", "pass", "anthropic"), _lane("done", "pass", "anthropic", "verify-blackbox"),
            _lane("done", "pass", "openai"), _lane("done", "pass", "openai", "verify-blackbox"),
        ])
        self.assertEqual((res["consensus"], res["merge_allowed"], res["has_disagreement"]), ("pass", True, False))
        self.assertIsNone(res["disagreement_summary"])
        self.assertEqual(res["vendors"], ["anthropic", "openai"])
        self.assertEqual((res["lane_count"], res["completed_count"]), (4, 4))

    def test_pass_and_reject_disagreement(self):
        res = self.agg([
            _lane("done", "pass", "anthropic", task_id="t_a"),
            _lane("done", "reject", "openai", task_id="t_o", findings=["SQL injection in /items"],
                  disclosed_limits=["no GPU"]),
        ])
        self.assertEqual((res["consensus"], res["merge_allowed"], res["has_disagreement"]), ("reject", False, True))
        self.assertEqual(res["verdicts"]["pass"], ["verify-diff/anthropic (t_a)"])
        self.assertEqual(res["verdicts"]["reject"], ["verify-diff/openai (t_o)"])
        self.assertIn("pass: verify-diff/anthropic (t_a)", res["disagreement_summary"])
        self.assertIn("reject: verify-diff/openai (t_o)", res["disagreement_summary"])
        self.assertEqual(res["findings"][0]["item"], "SQL injection in /items")
        self.assertEqual(res["findings"][0]["vendor"], "openai")
        self.assertEqual(res["disclosed_limits"][0]["item"], "no GPU")
        for verdict in ("fail", "block", "changes_requested"):
            with self.subTest(verdict=verdict):
                self.assertEqual(self.agg([_lane("done", "pass", "a"), _lane("done", verdict, "openai")])["consensus"], "reject")

    def test_pass_and_inconclusive(self):
        res = self.agg([_lane("done", "pass", "anthropic"), _lane("done", "inconclusive", "openai")])
        self.assertEqual((res["consensus"], res["merge_allowed"], res["has_disagreement"]), ("inconclusive", False, True))
        self.assertIn("inconclusive", res["disagreement_summary"])
        unknown = self.agg([_lane("done", "pass", "anthropic"), _lane("done", "looks fine", "openai")])
        self.assertEqual(unknown["consensus"], "inconclusive")

    def test_disagreement_summary_quotes_dissenting_evidence(self):
        res = self.agg([
            _lane("done", "pass", "anthropic", task_id="t_a"),
            _lane("done", "reject", "openai", task_id="t_o", findings=[
                {"severity": "blocking", "issue": "Missing auth check on DELETE /items"},
                "Token logged in plaintext",
            ]),
        ])
        summary = res["disagreement_summary"]
        self.assertIn("pass: verify-diff/anthropic (t_a)", summary)
        self.assertIn("reject: verify-diff/openai (t_o)", summary)
        self.assertIn("Missing auth check on DELETE /items", summary)
        self.assertIn("Token logged in plaintext", summary)

        res = self.agg([
            _lane("done", "pass", "anthropic", task_id="t_a"),
            _lane("done", "inconclusive", "openai", "verify-blackbox", task_id="t_b",
                  disclosed_limits=["Could not start the browser sandbox"]),
        ])
        summary = res["disagreement_summary"]
        self.assertIn("inconclusive: verify-blackbox/openai (t_b)", summary)
        self.assertIn("Could not start the browser sandbox", summary)

        titled = self.agg([
            _lane("done", "pass", "anthropic"),
            _lane("done", "reject", "openai", findings=[{"title": "Race in retry loop"}, {"message": "m1"}]),
        ])
        self.assertIn("Race in retry loop", titled["disagreement_summary"])
        self.assertIn("m1", titled["disagreement_summary"])

    def test_disagreement_summary_bounds_many_findings(self):
        many = [f"finding number {i} " + "x" * 400 for i in range(12)]
        res = self.agg([_lane("done", "pass", "anthropic"), _lane("done", "reject", "openai", findings=many)])
        summary = res["disagreement_summary"]
        self.assertIn("finding number 0", summary)
        self.assertNotIn("finding number 11", summary)
        self.assertIn("more", summary)
        self.assertLess(len(summary), 1500)
        self.assertEqual(len(res["findings"]), 12)

    def test_reject_wins_over_inconclusive_and_pending(self):
        res = self.agg([
            _lane("done", "inconclusive", "anthropic"), _lane("blocked", "reject", "openai"), _lane("running", None, "google"),
        ])
        self.assertEqual((res["consensus"], res["merge_allowed"]), ("reject", False))
        self.assertEqual(len(res["verdicts"]["pending"]), 1)

    def test_pending(self):
        res = self.agg([_lane("done", "pass", "anthropic"), _lane("running", None, "openai")])
        self.assertEqual((res["consensus"], res["merge_allowed"], res["has_disagreement"]), ("pending", False, False))
        self.assertEqual(res["completed_count"], 1)
        res = self.agg([_lane("done", "pass", "anthropic"), _lane("blocked", None, "openai")])
        self.assertEqual(res["consensus"], "pending")

    def test_degenerate_single_vendor_and_empty(self):
        res = self.agg([_lane("done", "pass", "anthropic"), _lane("done", "pass", "anthropic", "verify-blackbox")])
        self.assertEqual((res["consensus"], res["merge_allowed"]), ("inconclusive", False))
        self.assertIn("degenerate", res["reason"])
        self.assertEqual(self.agg([_lane("done", "pass", "anthropic")])["consensus"], "inconclusive")
        res = self.agg([_lane("done", "pass", "unknown"), _lane("done", "pass", None)])
        self.assertEqual(res["consensus"], "inconclusive")
        self.assertEqual(self.agg([])["consensus"], "inconclusive")


class TestEvaluatePanel(unittest.TestCase):
    def _spawn(self, home: Path, tenant: str, pr: int) -> dict[str, Any]:
        from factonio.orchestrator import spawn_verification_pair

        return spawn_verification_pair("acme/svc", tenant, pr, rigor="high", panel_models=TWO_VENDORS)

    def test_pending_then_pass_records_once(self):
        from factonio.orchestrator import evaluate_verification_panel

        with isolated_hermes_home() as home:
            _register(home)
            res = self._spawn(home, "t-ev", 9)
            pid = res["panel"]["panel_id"]
            lanes = res["panel"]["lanes"]

            ev = evaluate_verification_panel(panel_id=pid)
            self.assertTrue(ev["ok"], ev)
            self.assertEqual((ev["consensus"], ev["merge_allowed"], ev["recorded"]), ("pending", False, False))
            self.assertEqual(ev["lane_count"], 4)
            self.assertEqual(ev["tenant"], "t-ev")
            self.assertEqual(ev["pr"]["number"], 9)
            self.assertEqual(_trail_rows("verify_panel_consensus"), [])

            for lane in lanes:
                _complete(lane["verify_diff_task_id"], "pass")
                _complete(lane["verify_blackbox_task_id"], "pass", scenarios_run=3)
            ev = evaluate_verification_panel(tenant="t-ev", pr_number=9, record_trail=False)
            self.assertEqual((ev["consensus"], ev["merge_allowed"], ev["recorded"]), ("pass", True, False))
            self.assertEqual(_trail_rows("verify_panel_consensus"), [])

            ev = evaluate_verification_panel(panel_id=pid)
            self.assertEqual((ev["consensus"], ev["recorded"]), ("pass", True))
            rows = _trail_rows("verify_panel_consensus", "t-ev")
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0]["result"], rows[0]["phase"]), ("pass", "verify"))
            self.assertEqual(rows[0]["evidence"]["panel_id"], pid)
            self.assertEqual(rows[0]["evidence"]["vendors"], ["anthropic", "openai"])
            self.assertEqual(len(rows[0]["evidence"]["lanes"]), 4)
            self.assertFalse(rows[0]["evidence"]["has_disagreement"])

            again = evaluate_verification_panel(panel_id=pid)
            self.assertFalse(again["recorded"])
            self.assertEqual(again["decision_id"], rows[0]["id"])
            self.assertEqual(len(_trail_rows("verify_panel_consensus")), 1)

    def test_reject_disagreement_selection_and_errors(self):
        from factonio.orchestrator import evaluate_verification_panel

        with isolated_hermes_home() as home:
            _register(home)
            first = self._spawn(home, "t-rj", 11)
            second = self._spawn(home, "t-rj", 12)
            lanes = second["panel"]["lanes"]
            _complete(lanes[0]["verify_diff_task_id"], "pass")
            _complete(lanes[0]["verify_blackbox_task_id"], "pass")
            _complete(lanes[1]["verify_diff_task_id"], "reject", findings=["missing auth check"])
            _complete(lanes[1]["verify_blackbox_task_id"], "pass")

            ev = evaluate_verification_panel(tenant="t-rj", pr_number=12)
            self.assertEqual(ev["panel_id"], second["panel"]["panel_id"])
            self.assertEqual((ev["consensus"], ev["merge_allowed"], ev["has_disagreement"]), ("reject", False, True))
            self.assertIn("reject: verify-diff/openai", ev["disagreement_summary"])
            self.assertEqual([f["item"] for f in ev["findings"]], ["missing auth check"])
            self.assertTrue(ev["recorded"])
            row = _trail_rows("verify_panel_consensus", "t-rj")[0]
            self.assertEqual(row["result"], "reject")
            self.assertTrue(row["evidence"]["has_disagreement"])
            self.assertIn("disagree", row["why"])

            latest = evaluate_verification_panel(tenant="t-rj", record_trail=False)
            self.assertEqual(latest["panel_id"], second["panel"]["panel_id"])
            self.assertEqual(latest["other_panel_ids"], [first["panel"]["panel_id"]])
            older = evaluate_verification_panel(tenant="t-rj", pr_number=11, record_trail=False)
            self.assertEqual((older["panel_id"], older["consensus"]), (first["panel"]["panel_id"], "pending"))

            missing = evaluate_verification_panel(panel_id="vp_0000000000")
            self.assertFalse(missing["ok"])
            self.assertIn("No verification panel", missing["error"])
            with self.assertRaises(ValueError):
                evaluate_verification_panel()

    def test_completed_lanes_without_verdict_fail_closed(self):
        from factonio.metrics import compute_metrics
        from factonio.orchestrator import evaluate_verification_panel, workstream_status

        with isolated_hermes_home() as home:
            _register(home)
            res = self._spawn(home, "t-nv", 31)
            pid = res["panel"]["panel_id"]
            for lane in res["panel"]["lanes"]:
                _complete(lane["verify_diff_task_id"], None)
                _complete(lane["verify_blackbox_task_id"], None)
                self.assertEqual(_get_task(lane["verify_diff_task_id"]).status, "done")

            ev = evaluate_verification_panel(panel_id=pid, record_trail=True)
            self.assertTrue(ev["ok"], ev)
            self.assertEqual((ev["consensus"], ev["merge_allowed"], ev["recorded"]), ("pending", False, False))
            self.assertEqual(ev["lane_count"], 4)
            self.assertEqual(ev["completed_count"], 0)
            self.assertEqual(_trail_rows("verify_panel_consensus"), [])

            panels = workstream_status(tenant="t-nv")["panels"]
            self.assertEqual([(p["panel_id"], p["consensus"], p["merge_allowed"]) for p in panels], [(pid, "pending", False)])
            m = compute_metrics(home / "kanban.db", range_key="all", tenant="t-nv")
            self.assertEqual(m["verification_panels"]["pass"], 0)
            self.assertEqual(m["verification_panels"]["pending"], 1)

    def test_disagreement_summary_carries_dissenting_evidence(self):
        from factonio.orchestrator import evaluate_verification_panel

        with isolated_hermes_home() as home:
            _register(home)
            rej = self._spawn(home, "t-ds", 41)
            lanes = rej["panel"]["lanes"]
            _complete(lanes[0]["verify_diff_task_id"], "pass")
            _complete(lanes[0]["verify_blackbox_task_id"], "pass")
            _complete(lanes[1]["verify_diff_task_id"], "reject", findings=[
                {"severity": "blocking", "issue": "Unvalidated redirect in /login"}, "Secret echoed to logs",
            ])
            _complete(lanes[1]["verify_blackbox_task_id"], "pass")
            ev = evaluate_verification_panel(panel_id=rej["panel"]["panel_id"], record_trail=False)
            summary = ev["disagreement_summary"]
            self.assertEqual(ev["consensus"], "reject")
            self.assertIn("pass: verify-diff/anthropic", summary)
            self.assertIn("reject: verify-diff/openai", summary)
            self.assertIn("Unvalidated redirect in /login", summary)
            self.assertIn("Secret echoed to logs", summary)

            inc = self._spawn(home, "t-ds", 42)
            lanes = inc["panel"]["lanes"]
            for lane in lanes:
                _complete(lane["verify_diff_task_id"], "pass")
            _complete(lanes[0]["verify_blackbox_task_id"], "pass")
            _complete(lanes[1]["verify_blackbox_task_id"], "inconclusive",
                      disclosed_limits=["Headless browser unavailable in sandbox"])
            ev = evaluate_verification_panel(panel_id=inc["panel"]["panel_id"], record_trail=False)
            summary = ev["disagreement_summary"]
            self.assertEqual(ev["consensus"], "inconclusive")
            self.assertIn("pass: verify-diff/anthropic", summary)
            self.assertIn("inconclusive: verify-blackbox/openai", summary)
            self.assertIn("Headless browser unavailable in sandbox", summary)

    def test_panel_tag_identity_ignores_model_label(self):
        from factonio.metrics import parse_panel_tag

        stamp = "- Verification panel: `vp_abc123` (rigor=`high`, vendor=`anthropic`, model=`{}`)\n"
        for label, model in (("`Claude` (opus)", "`Claude` (opus)"), ("x`)`y", "x`)`y"), ("", "")):
            with self.subTest(label=label):
                tag = parse_panel_tag("intro\n" + stamp.format(label) + "tail `z`)")
                self.assertEqual(
                    tag, {"panel_id": "vp_abc123", "rigor": "high", "vendor": "anthropic", "model": model}
                )
        legacy = parse_panel_tag("- Verification panel: `vp_abc123` (rigor=`high`, vendor=`openai`)")
        self.assertEqual((legacy or {}).get("vendor"), "openai")
        self.assertIsNone((legacy or {}).get("model"))
        self.assertIsNone(parse_panel_tag("- Verification p4nel: `vp_abc123` (rigor=`high`, vendor=`openai`)"))

    def test_delimiter_bearing_labels_keep_every_lane(self):
        from factonio.orchestrator import evaluate_verification_panel, spawn_verification_pair

        with isolated_hermes_home() as home:
            _register(home)
            spawn = spawn_verification_pair("acme/svc", "t-lbl", 90, rigor="high", panel_models=[
                {"provider": "anthropic", "model": "claude-opus-4-6", "label": "`Claude`"},
                {"provider": "openai", "model": "gpt-5", "label": "GPT (v5)\nalt"},
                {"provider": "google", "model": "gemini-2.5-pro", "label": "Gemini"},
            ])
            self.assertTrue(spawn["ok"], spawn)
            lanes = spawn["panel"]["lanes"]
            self.assertEqual([lane["vendor"] for lane in lanes], ["anthropic", "openai", "google"])
            for lane in lanes:
                verdict = "reject" if lane["vendor"] == "anthropic" else "pass"
                findings = ["Critical auth defect"] if verdict == "reject" else []
                for key in ("verify_diff_task_id", "verify_blackbox_task_id"):
                    body = _get_task(lane[key]).body
                    stamp = next(line for line in body.splitlines() if "Verification panel:" in line)
                    self.assertNotIn("\n", stamp)
                    _complete(lane[key], verdict, findings=findings)

            res = evaluate_verification_panel(panel_id=spawn["panel"]["panel_id"], record_trail=False)
            self.assertEqual(res["lane_count"], 6)
            self.assertEqual((res["consensus"], res["merge_allowed"]), ("reject", False))
            self.assertEqual([f["item"] for f in res["findings"]], ["Critical auth defect"] * 2)
            self.assertEqual({f["vendor"] for f in res["findings"]}, {"anthropic"})
            self.assertIn("reject: verify-diff/anthropic", res["disagreement_summary"])
            self.assertIn("Critical auth defect", res["disagreement_summary"])

    def test_missing_expected_lane_fails_closed(self):
        from hermes_cli.kanban_db_connect import connect_closing

        from factonio.orchestrator import evaluate_verification_panel, spawn_verification_pair, workstream_status

        with isolated_hermes_home() as home:
            _register(home)
            spawn = spawn_verification_pair("acme/svc", "t-gap", 91, rigor="high", panel_models=[
                "anthropic/claude-opus-4-6", "openai/gpt-5", "google/gemini-2.5-pro",
            ])
            pid = spawn["panel"]["panel_id"]
            lanes = spawn["panel"]["lanes"]
            dropped = lanes[0]["verify_diff_task_id"]
            with connect_closing() as conn:
                conn.execute(
                    "UPDATE tasks SET body = replace(body, 'Verification panel:', 'Verification p4nel:') WHERE id = ?",
                    (dropped,),
                )
                conn.commit()
            _complete(dropped, "reject", findings=["hidden defect"])
            for lane in lanes:
                for key in ("verify_diff_task_id", "verify_blackbox_task_id"):
                    if lane[key] != dropped:
                        _complete(lane[key], "pass")

            ev = evaluate_verification_panel(panel_id=pid)
            self.assertTrue(ev["ok"], ev)
            self.assertEqual(ev["lane_count"], 5)
            self.assertFalse(ev["merge_allowed"])
            self.assertNotEqual(ev["consensus"], "pass")
            self.assertEqual(ev["consensus"], "inconclusive")
            self.assertEqual(ev["expected_lane_count"], 6)
            self.assertEqual(ev["missing_lane_ids"], [dropped])
            self.assertIn(dropped, ev["disagreement_summary"])
            self.assertTrue(any(dropped in str(lim["item"]) for lim in ev["disclosed_limits"]))
            row = _trail_rows("verify_panel_consensus", "t-gap")[0]
            self.assertEqual(row["result"], "inconclusive")
            self.assertFalse(row["evidence"]["merge_allowed"])

            by_pr = evaluate_verification_panel(tenant="t-gap", pr_number=91, record_trail=False)
            self.assertEqual((by_pr["consensus"], by_pr["merge_allowed"]), ("inconclusive", False))
            panels = workstream_status(tenant="t-gap")["panels"]
            self.assertEqual([(p["panel_id"], p["merge_allowed"]) for p in panels], [(pid, False)])

    def test_tool_and_cli(self):
        import hermes_yaml as yaml

        with isolated_hermes_home() as home:
            _register(home)
            res = self._spawn(home, "t-cli", 21)
            pid = res["panel"]["panel_id"]
            for lane in res["panel"]["lanes"]:
                _complete(lane["verify_diff_task_id"], "pass")
                _complete(lane["verify_blackbox_task_id"], "inconclusive", disclosed_limits=["no browser"])

            tools = _tools()
            self.assertIn("factonio_evaluate_panel", tools)
            manifest = yaml.safe_load((PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8"))
            self.assertIn("factonio_evaluate_panel", manifest["provides_tools"])
            self.assertEqual(sorted(manifest["provides_tools"]), sorted(tools))
            handler = tools["factonio_evaluate_panel"]["handler"]
            out = json.loads(handler({"panel_id": pid, "record": False}))
            self.assertTrue(out["ok"], out)
            self.assertEqual((out["consensus"], out["merge_allowed"], out["recorded"]), ("inconclusive", False, False))
            self.assertEqual(len(out["disclosed_limits"]), 2)
            self.assertFalse(json.loads(handler({}))["ok"])

            run = _cli()
            code, text, _ = run("panel", pid, "--json")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(text)["consensus"], "inconclusive")
            self.assertEqual(_trail_rows("verify_panel_consensus"), [])
            code, text, _ = run("panel", "--tenant", "t-cli", "--pr", "21", "--record")
            self.assertEqual(code, 0)
            self.assertIn(f"INCONCLUSIVE: panel {pid} acme/svc#21 (merge blocked)", text)
            self.assertIn("Disagreement:", text)
            self.assertIn("Recorded verify_panel_consensus", text)
            self.assertEqual(len(_trail_rows("verify_panel_consensus")), 1)
            code, _, err = run("panel", "vp_ffffffffff")
            self.assertEqual(code, 1)
            self.assertIn("No verification panel", err)
            code, _, err = run("panel")
            self.assertEqual(code, 1)


class TestMetricsAndStatus(unittest.TestCase):
    def test_additive_panel_metrics_and_status(self):
        from factonio.metrics import compute_metrics, thread_cards
        from factonio.orchestrator import spawn_verification_pair, workstream_status

        with isolated_hermes_home() as home:
            _register(home)
            std = spawn_verification_pair("acme/svc", "t-m", 1)
            hi = spawn_verification_pair("acme/svc", "t-m", 2, rigor="high", panel_models=TWO_VENDORS)
            rej = spawn_verification_pair("acme/svc", "t-m", 3, rigor="high", panel_models=TWO_VENDORS)
            for lane in hi["panel"]["lanes"]:
                _complete(lane["verify_diff_task_id"], "pass")
                _complete(lane["verify_blackbox_task_id"], "pass")
            _complete(rej["panel"]["lanes"][0]["verify_diff_task_id"], "pass")
            _complete(rej["panel"]["lanes"][1]["verify_diff_task_id"], "reject")
            _complete(std["verify_diff_task_id"], "pass")

            db = home / "kanban.db"
            cards = thread_cards(db, tenant="t-m")
            self.assertEqual(set(cards), {"cards", "counts", "workstreams", "arenas"})
            self.assertEqual(cards["counts"]["total"], 10)
            by_id = {c["id"]: c for c in cards["cards"]}
            self.assertIsNone(by_id[std["verify_diff_task_id"]]["panel_id"])
            self.assertIsNone(by_id[std["verify_diff_task_id"]]["panel_vendor"])
            first = by_id[hi["verify_diff_task_id"]]
            self.assertEqual((first["panel_id"], first["panel_vendor"]), (hi["panel"]["panel_id"], "anthropic"))
            for key in ("id", "title", "status", "phase", "verdict", "pr", "runs", "summary", "last_error"):
                self.assertIn(key, first)

            status = workstream_status(tenant="t-m")
            self.assertTrue(status["ok"])
            self.assertIn("counts", status)
            panels = {p["panel_id"]: p for p in status["panels"]}
            self.assertEqual(set(panels), {hi["panel"]["panel_id"], rej["panel"]["panel_id"]})
            self.assertEqual(panels[hi["panel"]["panel_id"]]["consensus"], "pass")
            self.assertTrue(panels[hi["panel"]["panel_id"]]["merge_allowed"])
            self.assertEqual(panels[rej["panel"]["panel_id"]]["consensus"], "reject")
            self.assertTrue(panels[rej["panel"]["panel_id"]]["has_disagreement"])

            m = compute_metrics(db, range_key="all", tenant="t-m")
            for key in ("range", "tenant", "totals", "phases", "verification", "rework", "escapes", "trail"):
                self.assertIn(key, m)
            self.assertEqual(set(m["verification"]), {"diff", "blackbox"})
            self.assertEqual(m["verification"]["diff"]["pass"], 4)
            self.assertEqual(m["verification"]["diff"]["reject"], 1)
            self.assertEqual(
                m["verification_panels"],
                {"total": 2, "pass": 1, "reject": 1, "inconclusive": 0, "pending": 0, "disagreements": 1},
            )

            other = workstream_status(tenant="nobody")
            self.assertEqual(other["panels"], [])

    def test_empty_db_has_zero_panel_block(self):
        from factonio.metrics import compute_metrics

        with isolated_hermes_home() as home:
            m = compute_metrics(home / "missing.db", range_key="30d")
            self.assertEqual(
                m["verification_panels"],
                {"total": 0, "pass": 0, "reject": 0, "inconclusive": 0, "pending": 0, "disagreements": 0},
            )


if __name__ == "__main__":
    unittest.main()
