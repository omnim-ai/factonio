"""Clean-room tests for the append-only `trail.db` decision trail, its API/CLI/tool surfaces,
automatic transition logging, the `/correct` retro prompt, escaped-regression gap analysis,
vendor-family cross-model warnings, and Wave 2 attribution."""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tests._env import PLUGIN_ROOT, isolated_hermes_home

if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))


class _FakeCtx:
    def __init__(self) -> None:
        self.cli: dict[str, Any] = {}
        self.tools: dict[str, dict[str, Any]] = {}

    def register_cli_command(self, name: str, *, help: str, setup_fn: Any, handler_fn: Any) -> None:
        self.cli[name] = {"setup_fn": setup_fn, "handler_fn": handler_fn}

    def register_tool(self, *, name: str, toolset: str, schema: dict, handler: Any) -> None:
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler}


@contextlib.contextmanager
def _no_worker_env():
    """Hide the outer Kanban worker's tenant/card so tests observe their own defaults."""
    saved = {k: os.environ.pop(k, None) for k in ("HERMES_TENANT", "HERMES_KANBAN_TASK")}
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def _register_repo(home: Path, spec: str = "acme/sample") -> None:
    from factonio.repos import add_repo

    local = home / "dev" / spec.split("/")[1]
    (local / ".git").mkdir(parents=True, exist_ok=True)
    add_repo(spec, local_path=str(local), clone=False, inspect_remote=False)


def _tools() -> dict[str, dict[str, Any]]:
    from factonio.agent_tools import register_tools

    ctx = _FakeCtx()
    register_tools(ctx)
    return ctx.tools


def _api_client(name: str):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    spec = importlib.util.spec_from_file_location(name, PLUGIN_ROOT / "dashboard" / "plugin_api.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    app = FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/factonio")
    return TestClient(app)


class TestTrailStore(unittest.TestCase):
    def test_store_location_pragmas_schema_and_indexes(self):
        from factonio import trail

        with isolated_hermes_home() as home:
            profile_home = home / "profiles" / "factonio"
            profile_home.mkdir(parents=True)
            with mock.patch.dict(os.environ, {"HERMES_HOME": str(profile_home)}):
                path = trail.trail_db_path()
                self.assertEqual(path, home / "plugin-data" / "factonio" / "trail.db")
                trail.log_decision("probe", tenant="t1")
            self.assertTrue(path.is_file())

            with contextlib.closing(trail._connect()) as conn:
                self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
                self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
                self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                meta = dict(conn.execute("SELECT key, value FROM schema_meta").fetchall())
                self.assertEqual(meta["schema_version"], "1")
                cols = [r[1] for r in conn.execute("PRAGMA table_info(decisions)").fetchall()]
                self.assertEqual(
                    cols,
                    ["id", "ts", "tenant", "repo_slug", "phase", "card_id", "actor", "decision", "why",
                     "evidence_json", "result", "supersedes_id"],
                )
                triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
                self.assertEqual(triggers, {"trg_decisions_no_update", "trg_decisions_no_delete"})
                indexed = set()
                for (idx,) in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='decisions' AND sql IS NOT NULL"
                ):
                    indexed.add(tuple(r[2] for r in conn.execute(f"PRAGMA index_info({idx})")))
                self.assertTrue(
                    {("tenant", "ts"), ("repo_slug", "ts"), ("card_id",), ("supersedes_id",)} <= indexed, indexed
                )

    def test_rows_are_immutable_and_corrections_supersede(self):
        from factonio import trail

        with isolated_hermes_home():
            first = trail.log_decision(
                "choose_store", tenant="t1", phase="define", why="kanban.db is fenced", evidence={"pr_number": 3}
            )
            self.assertRegex(first["id"], r"^d_[0-9a-f]{12}$")
            self.assertRegex(first["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z$")
            self.assertEqual(first["actor"], "orchestrator")
            self.assertEqual(first["evidence"], {"pr_number": 3})
            self.assertEqual((first["superseded_by"], first["is_superseded"]), ([], False))

            with contextlib.closing(sqlite3.connect(str(trail.trail_db_path()), isolation_level=None)) as raw:
                with self.assertRaisesRegex(sqlite3.DatabaseError, "append-only"):
                    raw.execute("UPDATE decisions SET why = 'rewritten' WHERE id = ?", (first["id"],))
                with self.assertRaisesRegex(sqlite3.DatabaseError, "append-only"):
                    raw.execute("DELETE FROM decisions WHERE id = ?", (first["id"],))
                raw.execute("PRAGMA foreign_keys=ON")
                with self.assertRaises(sqlite3.IntegrityError):
                    raw.execute(
                        "INSERT INTO decisions(id, ts, decision, supersedes_id) VALUES ('d_x', 'now', 'x', 'd_missing')"
                    )

            with self.assertRaisesRegex(ValueError, "does not reference"):
                trail.log_decision("fix", supersedes_id="d_000000000000")
            with self.assertRaisesRegex(ValueError, "non-empty"):
                trail.log_decision("  ")
            with self.assertRaisesRegex(ValueError, "JSON object"):
                trail.log_decision("x", evidence=["not", "a", "dict"])  # type: ignore[arg-type]

            fix = trail.log_decision("choose_store_v2", tenant="t1", phase="define", supersedes_id=first["id"])
            rows = {r["id"]: r for r in trail.query_decisions(tenant="t1")}
            self.assertEqual(rows[first["id"]]["why"], "kanban.db is fenced")
            self.assertEqual(rows[first["id"]]["superseded_by"], [fix["id"]])
            self.assertTrue(rows[first["id"]]["is_superseded"])
            self.assertEqual(rows[fix["id"]]["supersedes_id"], first["id"])
            self.assertFalse(rows[fix["id"]]["is_superseded"])

            summary = trail.summarize_trail(tenant="t1")
            self.assertEqual((summary["total"], summary["active"], summary["superseded"]), (2, 1, 1))

    def test_query_filters_limit_summary_and_renderers(self):
        from factonio import trail

        with isolated_hermes_home() as home:
            self.assertEqual(trail.query_decisions(), [])
            self.assertEqual(trail.summarize_trail()["total"], 0)
            self.assertFalse(trail.trail_db_path().exists(), "reads must not create trail.db")

            _register_repo(home)
            trail.log_decision("a", tenant="t1", repo_slug="acme/sample", phase="define", card_id="t_1")
            trail.log_decision("b", tenant="t1", repo_slug="acme-sample", phase="implement", card_id="t_2",
                               actor="worker", evidence={"head_sha": "abc"}, result="spawned")
            trail.log_decision("c|pipe", tenant="t2", repo_slug="other", phase="implement", why="line1\nline2")

            self.assertEqual([r["decision"] for r in trail.query_decisions()], ["c|pipe", "b", "a"])
            self.assertEqual({r["repo_slug"] for r in trail.query_decisions(repo="acme/sample")}, {"acme-sample"})
            self.assertEqual(len(trail.query_decisions(repo="acme-sample")), 2)
            self.assertEqual([r["decision"] for r in trail.query_decisions(phase="implement", tenant="t1")], ["b"])
            self.assertEqual([r["decision"] for r in trail.query_decisions(card_id="t_1")], ["a"])
            self.assertEqual(len(trail.query_decisions(limit=1)), 1)

            payload = trail.trail_payload(tenant="t1", limit=1)
            self.assertEqual(set(payload), {"decisions", "summary", "filters"})
            self.assertEqual(len(payload["decisions"]), 1)
            self.assertEqual(payload["summary"]["total"], 2, "summary is not truncated by limit")
            self.assertEqual(payload["summary"]["by_phase"], {"define": 1, "implement": 1})
            self.assertEqual(payload["summary"]["by_actor"], {"orchestrator": 1, "worker": 1})
            self.assertEqual(payload["filters"], {"tenant": "t1", "repo": None, "phase": None, "card_id": None, "limit": 1})

            rows = trail.query_decisions()
            text = trail.render_trail_text(rows)
            self.assertIn("worker: b -> spawned", text)
            self.assertIn("evidence: head_sha=abc", text)
            md = trail.render_trail_markdown(rows)
            self.assertTrue(md.startswith("## Decision Trail"))
            self.assertIn("c\\|pipe", md)
            self.assertIn("line1 line2", md)
            self.assertEqual(len([ln for ln in md.splitlines() if ln.startswith("| ")]), 4)
            self.assertIn("No decisions", trail.render_trail_text([]))
            self.assertIn("_No decisions recorded._", trail.render_trail_markdown([]))


class TestAutomaticTransitions(unittest.TestCase):
    def test_orchestrator_transitions_and_escape_gap_analysis(self):
        from hermes_cli import kanban_db as kb
        from hermes_cli.kanban_db_connect import connect_closing
        from factonio import trail
        from factonio.orchestrator import (
            spawn_design_card,
            spawn_implementation_card,
            spawn_verification_pair,
            start_workstream,
        )

        with isolated_hermes_home() as home:
            _register_repo(home)
            plain = start_workstream("acme/sample", "Add caching", kind="feature", issue_number=5)
            with connect_closing() as conn:
                plain_task = kb.get_task(conn, plain["task_id"])
            assert plain_task is not None
            plain_body = plain_task.body or ""
            self.assertNotIn("Verification Gap Analysis", plain_body)

            started = start_workstream(
                "acme/sample", "Duplicate webhooks double-credit", kind="bug", issue_number=18,
                escaped_from="t_verify9",
            )
            with connect_closing() as conn:
                escape_task = kb.get_task(conn, started["task_id"])
            assert escape_task is not None
            body = escape_task.body or ""
            self.assertIn("## Verification Gap Analysis (required)", body)
            self.assertIn("`t_verify9`", body)
            self.assertIn("Architecture -> Types -> Lint/CI check naming the fix -> Behavioral test -> Skill text last", body)
            tenant = started["tenant"]

            design = spawn_design_card("acme/sample", tenant, provider="hermes", parents=[started["task_id"]])
            impl = spawn_implementation_card("acme/sample", tenant, "Idempotency keys", "Dedupe by delivery id")
            ver = spawn_verification_pair("acme/sample", tenant, 21, impl_card_id=impl["task_id"], head_sha="abc1234def")
            spawn_design_card("acme/sample", tenant, provider="off")

            rows = list(reversed(trail.query_decisions(tenant=tenant)))
            self.assertEqual(
                [(r["decision"], r["phase"]) for r in rows],
                [
                    ("start_workstream", "define"),
                    ("escaped_regression", "escape"),
                    ("spawn_design", "design"),
                    ("spawn_implement", "implement"),
                    ("spawn_verify", "verify"),
                    ("skip_design", "design"),
                ],
            )
            self.assertTrue(all(r["repo_slug"] == "acme-sample" for r in rows))
            self.assertEqual(rows[0]["card_id"], started["task_id"])
            self.assertEqual(rows[1]["evidence"]["escaped_from"], "t_verify9")
            self.assertEqual(rows[2]["card_id"], design["task_id"])
            self.assertEqual(rows[3]["card_id"], impl["task_id"])
            self.assertEqual(rows[4]["card_id"], impl["task_id"])
            self.assertEqual(rows[4]["evidence"]["pr_number"], 21)
            self.assertEqual(rows[4]["evidence"]["verify_diff_task_id"], ver["verify_diff_task_id"])
            self.assertEqual(rows[5]["result"], "skipped")
            self.assertEqual(len(trail.query_decisions(tenant=plain["tenant"])), 1)

    def test_trail_failure_never_breaks_card_creation(self):
        from factonio.orchestrator import start_workstream

        with isolated_hermes_home() as home:
            _register_repo(home)
            with mock.patch("factonio.trail.log_decision", side_effect=sqlite3.OperationalError("disk I/O error")):
                with self.assertLogs("factonio.trail", level="WARNING") as logs:
                    started = start_workstream("acme/sample", "Still works", kind="quick")
            self.assertTrue(started["ok"])
            self.assertIn("disk I/O error", "\n".join(logs.output))

    def test_pr_gate_and_merge_tools_record_outcomes(self):
        from factonio import trail

        gate_blocked = {
            "ok": True, "repo": "acme/sample", "pr_number": 21, "head_sha": "abc", "ready_to_merge": False,
            "blockers": ["CI check failed: unit"], "blocker_tier": 3,
            "checks": {"passed": ["lint"], "failed": ["unit"], "pending": []},
        }
        merged = {"ok": True, "merged": True, "queued": False, "repo": "acme/sample", "pr_number": 21,
                  "head_sha": "abc", "url": "https://github.com/acme/sample/pull/21", "method": "squash"}
        refused = {"ok": False, "merged": False, "queued": False, "error": "HEAD mismatch: expected zzz",
                   "gate": {"head_sha": "abc"}}

        with isolated_hermes_home() as home, _no_worker_env():
            _register_repo(home)
            tools = _tools()
            with mock.patch("factonio.agent_tools.inspect_pr_gate", return_value=gate_blocked):
                out = json.loads(tools["factonio_pr_gate"]["handler"](
                    {"repo": "acme/sample", "pr_number": 21, "tenant": "acme-sample-i18", "card_id": "t_impl"}))
            self.assertEqual(out["blocker_tier"], 3)
            with mock.patch("factonio.agent_tools.merge_pr", side_effect=[refused, merged]):
                tools["factonio_merge_pr"]["handler"](
                    {"repo": "acme/sample", "pr_number": 21, "expected_head_sha": "zzz", "tenant": "acme-sample-i18"})
                with mock.patch.dict(os.environ, {"HERMES_TENANT": "acme-sample-i18", "HERMES_KANBAN_TASK": "t_w"}):
                    tools["factonio_merge_pr"]["handler"]({"repo": "acme/sample", "pr_number": 21, "expected_head_sha": "abc"})

            rows = list(reversed(trail.query_decisions(tenant="acme-sample-i18")))
            self.assertEqual([(r["decision"], r["phase"], r["result"]) for r in rows], [
                ("pr_gate", "gate", "blocked"),
                ("merge_pr", "merge", "refused"),
                ("merge_pr", "merge", "merged"),
            ])
            gate_row = rows[0]
            self.assertEqual(gate_row["repo_slug"], "acme-sample")
            self.assertEqual(gate_row["card_id"], "t_impl")
            self.assertIn("CI check failed", gate_row["why"])
            self.assertEqual(
                (gate_row["evidence"]["checks_failed"], gate_row["evidence"]["blocker_tier"]), (1, 3)
            )
            self.assertIn("HEAD mismatch", rows[1]["why"])
            self.assertEqual(rows[1]["evidence"]["expected_head_sha"], "zzz")
            self.assertEqual((rows[2]["actor"], rows[2]["card_id"]), ("worker", "t_w"))


class TestLogDecisionTool(unittest.TestCase):
    def test_registered_and_declared_in_manifest(self):
        import hermes_yaml as yaml

        tools = _tools()
        self.assertIn("factonio_log_decision", tools)
        schema = tools["factonio_log_decision"]["schema"]
        self.assertEqual(schema["parameters"]["required"], ["decision", "why"])
        self.assertIn("supersedes_id", schema["parameters"]["properties"])
        manifest = yaml.safe_load((PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8"))
        self.assertEqual(sorted(manifest["provides_tools"]), sorted(tools))

    def test_tool_logs_supersedes_and_validates(self):
        from factonio import trail

        with isolated_hermes_home(), _no_worker_env():
            handler = _tools()["factonio_log_decision"]["handler"]
            first = json.loads(handler({
                "decision": "attack_the_premise", "why": "Both fixes assumed network access", "phase": "implement",
                "tenant": "t1", "card_id": "t_9", "evidence": {"probe": "curl -I https://x", "exit_code": 6},
                "result": "premise_false",
            }))
            self.assertTrue(first["ok"], first)
            row = first["decision"]
            self.assertEqual((row["actor"], row["evidence"]["exit_code"]), ("orchestrator", 6))

            second = json.loads(handler({
                "decision": "attack_the_premise", "why": "Probe was run on the wrong base",
                "supersedes_id": row["id"], "evidence": json.dumps({"base": "main"}),
            }))
            self.assertTrue(second["ok"], second)
            self.assertEqual(second["decision"]["evidence"], {"base": "main"})

            bad = json.loads(handler({"decision": "x", "why": "y", "supersedes_id": "d_nope"}))
            self.assertFalse(bad["ok"])
            self.assertIn("does not reference", bad["error"])

            with mock.patch.dict(os.environ, {"HERMES_TENANT": "t-env", "HERMES_KANBAN_TASK": "t_card"}):
                env_row = json.loads(handler({"decision": "countersign", "why": "both verdicts pass"}))["decision"]
            self.assertEqual((env_row["tenant"], env_row["card_id"], env_row["actor"]), ("t-env", "t_card", "worker"))
            self.assertTrue(next(r for r in trail.query_decisions(tenant="t1"))["is_superseded"])


class TestTrailCliAndApi(unittest.TestCase):
    def _cli(self):
        from factonio.commands import register_cli

        ctx = _FakeCtx()
        register_cli(ctx, PLUGIN_ROOT)
        entry = ctx.cli["factonio"]
        parser = argparse.ArgumentParser(prog="hermes factonio")
        entry["setup_fn"](parser)

        def run(*argv: str) -> tuple[int, str]:
            out = io.StringIO()
            code = 0
            with contextlib.redirect_stdout(out):
                try:
                    entry["handler_fn"](parser.parse_args(list(argv)))
                except SystemExit as exc:
                    code = int(exc.code or 0)
            return code, out.getvalue()

        return run

    def _seed(self) -> dict[str, Any]:
        from factonio import trail

        a = trail.log_decision("spawn_verify", tenant="acme-api-i42", repo_slug="acme-api", phase="verify",
                               card_id="t_abc123", why="PR #42 opened at abc1234",
                               evidence={"pr_number": 42, "head_sha": "abc1234"}, result="spawned")
        trail.log_decision("pr_gate", tenant="acme-api-i42", repo_slug="acme-api", phase="gate", result="blocked")
        trail.log_decision("spawn_verify", tenant="acme-api-i42", repo_slug="acme-api", phase="verify",
                           card_id="t_abc123", why="re-verify after rebase", supersedes_id=a["id"])
        trail.log_decision("start_workstream", tenant="other-i1", repo_slug="other", phase="define")
        return a

    def test_cli_trail_text_markdown_json(self):
        with isolated_hermes_home():
            run = self._cli()
            code, out = run("trail")
            self.assertEqual(code, 0)
            self.assertIn("total=0", out)

            first = self._seed()
            code, out = run("trail", "--tenant", "acme-api-i42")
            self.assertEqual(code, 0)
            self.assertIn("total=3 active=2 superseded=1", out)
            self.assertIn("superseded by", out)
            self.assertNotIn("other-i1", out)

            code, out = run("trail", "--phase", "verify", "--card", "t_abc123", "--markdown")
            self.assertEqual(code, 0)
            self.assertIn("## Decision Trail", out)
            self.assertIn("~~spawn_verify~~", out)
            self.assertIn(f"supersedes {first['id']}", out)

            for argv in (("trail", "--repo", "acme-api", "--limit", "2", "--json"), ("--json", "trail", "--repo", "acme-api", "--limit", "2")):
                code, out = run(*argv)
                self.assertEqual(code, 0)
                data = json.loads(out)
                self.assertEqual(len(data["decisions"]), 2)
                self.assertEqual(data["summary"]["total"], 3)
                self.assertEqual(data["filters"]["repo"], "acme-api")
                self.assertEqual(data["filters"]["limit"], 2)

    def test_trail_api_contract_and_metrics_summary(self):
        with isolated_hermes_home():
            client = _api_client("factonio_test_plugin_api_trail")
            empty = client.get("/api/plugins/factonio/trail").json()
            self.assertEqual(empty["decisions"], [])
            self.assertEqual(empty["filters"], {"tenant": None, "repo": None, "phase": None, "card_id": None, "limit": 100})
            self.assertEqual(empty["summary"]["total"], 0)
            m_empty = client.get("/api/plugins/factonio/metrics").json()
            self.assertEqual(
                m_empty["trail"],
                {"decision_count": 0, "active_count": 0, "superseded_count": 0, "by_tenant": {}, "by_phase": {}},
            )

            first = self._seed()
            resp = client.get("/api/plugins/factonio/trail", params={"tenant": "acme-api-i42", "phase": "verify"})
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(set(body), {"decisions", "summary", "filters"})
            row = next(d for d in body["decisions"] if d["id"] == first["id"])
            self.assertEqual(
                list(row),
                ["id", "ts", "tenant", "repo_slug", "phase", "card_id", "actor", "decision", "why", "evidence",
                 "result", "supersedes_id", "superseded_by", "is_superseded"],
            )
            self.assertEqual(row["evidence"], {"head_sha": "abc1234", "pr_number": 42})
            self.assertTrue(row["is_superseded"])
            self.assertEqual(len(row["superseded_by"]), 1)
            self.assertEqual(
                {k: body["summary"][k] for k in ("total", "active", "superseded", "by_phase", "by_actor", "by_tenant")},
                {"total": 2, "active": 1, "superseded": 1, "by_phase": {"verify": 2},
                 "by_actor": {"orchestrator": 2}, "by_tenant": {"acme-api-i42": 2}},
            )
            self.assertTrue(body["summary"]["latest_ts"].endswith("Z"))
            self.assertEqual(body["filters"]["phase"], "verify")

            by_card = client.get("/api/plugins/factonio/trail?card_id=t_abc123&limit=1").json()
            self.assertEqual((len(by_card["decisions"]), by_card["summary"]["total"]), (1, 2))
            self.assertEqual(client.get("/api/plugins/factonio/trail?limit=0").status_code, 422)

            metrics = client.get("/api/plugins/factonio/metrics?range=30d").json()
            for key in ("range", "tenant", "totals", "phases", "verification", "rework", "escapes"):
                self.assertIn(key, metrics)
            self.assertEqual(
                metrics["trail"],
                {"decision_count": 4, "active_count": 3, "superseded_count": 1,
                 "by_tenant": {"acme-api-i42": 3, "other-i1": 1},
                 "by_phase": {"define": 1, "gate": 1, "verify": 2}},
            )
            scoped = client.get("/api/plugins/factonio/metrics?tenant=other-i1").json()["trail"]
            self.assertEqual((scoped["decision_count"], scoped["by_tenant"]), (1, {"other-i1": 1}))
            cards = client.get("/api/plugins/factonio/cards").json()
            self.assertEqual(set(cards), {"cards", "counts"})

    def test_unreadable_trail_db_does_not_break_metrics(self):
        from factonio import trail
        from factonio.metrics import compute_metrics

        with isolated_hermes_home() as home:
            path = trail.trail_db_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"this is not a sqlite database" * 64)
            metrics = compute_metrics(home / "kanban.db")
            self.assertEqual(metrics["trail"]["decision_count"], 0)
            self.assertIn("totals", metrics)


class TestRetroAndSkills(unittest.TestCase):
    def test_retro_prompt_mines_trail_and_applies_correct_ladder(self):
        from factonio import RETRO_BLUEPRINT_PROMPT as prompt

        self.assertIn("factonio_status", prompt)
        self.assertIn("trail.db", prompt)
        self.assertIn("hermes factonio trail --json", prompt)
        self.assertIn(">= 2 occurrences", prompt)
        self.assertIn(
            "Architecture (make it unrepresentable) -> Types -> Lint/CI check whose failure message names the fix "
            "-> Behavioral test -> Skill text last",
            prompt,
        )
        self.assertIn("MUST be shown to fail on a real past failure", prompt)
        self.assertIn("factonio_log_decision", prompt)

    def test_skills_document_gap_analysis_and_ketchup_handoffs(self):
        define = (PLUGIN_ROOT / "skills" / "define" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("### 3b. Verification Gap Analysis", define)
        self.assertIn("`## Verification Gap Analysis`", define)
        orch = (PLUGIN_ROOT / "skills" / "orchestrator" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("## Human Handoffs (Ketchup)", orch)
        for phrase in ("Plain language first", "Visuals before prose", "Self-contained", "Evidence and risk"):
            self.assertIn(phrase, orch)
        self.assertIn("factonio_log_decision", orch)


class TestCrossVendorWarning(unittest.TestCase):
    def test_infer_model_vendor_across_routers(self):
        from factonio.config import infer_model_vendor

        cases = [
            (("anthropic", "claude-sonnet-4-5"), "anthropic"),
            (("openrouter", "anthropic/claude-opus-4"), "anthropic"),
            (("vertex", "claude-3-5-sonnet@20240620"), "anthropic"),
            (("bedrock", "us.anthropic.claude-3-7-sonnet-20250219-v1:0"), "anthropic"),
            (("vertex", "gemini-2.5-pro"), "google"),
            (("openrouter", "google/gemini-2.5-flash"), "google"),
            (("openai", "gpt-5.1"), "openai"),
            (("openrouter", "openai/o3-mini"), "openai"),
            (("openrouter", "meta-llama/llama-3.3-70b-instruct"), "meta"),
            (("openrouter", "x-ai/grok-4"), "xai"),
            (("openrouter", "moonshotai/kimi-k2"), "moonshot"),
            (("deepseek", "deepseek-chat"), "deepseek"),
            (("anthropic", "internal-alias"), "anthropic"),
            (("openrouter", "mystery-model"), None),
            ((None, "mystery-model"), None),
        ]
        for (provider, model), expected in cases:
            with self.subTest(provider=provider, model=model):
                self.assertEqual(infer_model_vendor(provider, model), expected)

    def _configure(self, home: Path, implement: dict[str, str], verify: dict[str, str]) -> None:
        from factonio.config import save_settings

        save_settings({"repos": {"acme-api": {
            "slug": "acme-api", "repo": "acme/api", "path": str(home / "acme-api"),
            "models": {"implement": implement, "verify": verify},
        }}})

    def test_warning_levels(self):
        from factonio.config import check_cross_model_warning

        cases = [
            ({"provider": "anthropic", "model": "claude-opus-4"}, {"provider": "anthropic", "model": "claude-opus-4"},
             "same model"),
            ({"provider": "anthropic", "model": "claude-opus-4"}, {"provider": "openrouter", "model": "anthropic/claude-opus-4"},
             "same model"),
            ({"provider": "anthropic", "model": "claude-opus-4"}, {"provider": "openrouter", "model": "anthropic/claude-sonnet-4"},
             "same vendor/model family (anthropic)"),
            ({"provider": "vertex", "model": "gemini-2.5-pro"}, {"provider": "openrouter", "model": "google/gemini-2.5-flash"},
             "same vendor/model family (google)"),
            ({"provider": "openai", "model": "gpt-5.1"}, {"provider": "openrouter", "model": "openai/o3"},
             "same vendor/model family (openai)"),
            ({"provider": "anthropic", "model": "claude-opus-4"}, {"provider": "openrouter", "model": "openai/gpt-5.1"}, None),
            ({"provider": "openrouter", "model": "mystery-a"}, {"provider": "openrouter", "model": "mystery-b"}, None),
        ]
        with isolated_hermes_home() as home:
            for implement, verify, expected in cases:
                with self.subTest(implement=implement, verify=verify):
                    self._configure(home, implement, verify)
                    warn = check_cross_model_warning(repo_slug="acme-api")
                    if expected is None:
                        self.assertIsNone(warn)
                    else:
                        self.assertIn(expected, warn or "")
                        self.assertIn("factonio_verify", warn or "")
                        if expected != "same model":
                            self.assertNotIn("same model as", warn or "")


class TestAttribution(unittest.TestCase):
    PATTERNS = ("show-me-your-work", "ketchup", "/correct", "worktree-audit.sh", "side-effect liveness")

    def test_readme_credits_wave2_patterns_and_documents_trail(self):
        readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
        ack = readme.split("## Acknowledgments", 1)[1].split("\n## ", 1)[0]
        self.assertIn("@poteto", ack)
        self.assertIn("pstack", ack)
        for pattern in self.PATTERNS:
            self.assertIn(pattern, ack)
        self.assertIn("hermes factonio trail", readme)
        self.assertIn("factonio_log_decision", readme)
        self.assertIn("/api/plugins/factonio/trail", readme)

    def test_notices_keep_full_mit_text_and_list_wave2_patterns(self):
        notices = (PLUGIN_ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        for pattern in self.PATTERNS:
            self.assertIn(pattern, notices)
        for clause in (
            "Copyright (c) 2026 Lauren Tan",
            "Permission is hereby granted, free of charge, to any person obtaining a copy",
            "The above copyright notice and this permission notice shall be included in all",
            'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR',
        ):
            self.assertIn(clause, notices)


if __name__ == "__main__":
    unittest.main()
