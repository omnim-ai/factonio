"""Clean-room tests for Wave 5 workstream modes (refactor, perf, investigate) and `hermes factonio start`."""

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


def _task(task_id: str) -> Any:
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing

    with connect_closing() as conn:
        task = kb.get_task(conn, task_id)
    assert task is not None
    return task


def _task_count() -> int:
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing

    with connect_closing() as conn:
        return len(kb.list_tasks(conn))


class TestWorkstreamModeConstants(unittest.TestCase):
    def test_kinds_and_benchmark_checklist(self):
        from factonio.orchestrator import (
            BENCHMARK_MEASUREMENT_MANTRAS,
            BENCHMARK_PRE_OPT_QUESTIONS,
            WORKSTREAM_KINDS,
        )

        self.assertEqual(WORKSTREAM_KINDS, ("feature", "bug", "quick", "refactor", "perf", "investigate"))
        for items in (BENCHMARK_PRE_OPT_QUESTIONS, BENCHMARK_MEASUREMENT_MANTRAS):
            self.assertIsInstance(items, tuple)
            self.assertEqual(len(items), 7)
            self.assertEqual(len(set(items)), 7)
            self.assertTrue(all(isinstance(i, str) and i.strip() for i in items))

    def test_investigation_report_path(self):
        from factonio.orchestrator import investigation_report_path, run_data_dir

        with isolated_hermes_home() as home:
            path = investigation_report_path("acme-sample-i3")
            self.assertEqual(path, run_data_dir("acme-sample-i3") / "investigation.md")
            self.assertTrue(path.is_relative_to(home))
            self.assertFalse(path.exists())


class TestStartWorkstreamModes(unittest.TestCase):
    def test_existing_kinds_unchanged_with_additive_keys(self):
        from factonio.orchestrator import start_workstream

        with isolated_hermes_home() as home:
            _register(home)
            feat = start_workstream("acme/sample", "Add caching", kind="feature", issue_number=1)
            bug = start_workstream("acme/sample", "Crash on empty body", kind="bug", issue_number=2)
            quick = start_workstream("acme/sample", "Fix typo", kind="quick", issue_number=3)

            for res, kind, phase in ((feat, "feature", "define"), (bug, "bug", "define"), (quick, "quick", "implement")):
                self.assertTrue(res["ok"])
                self.assertEqual(res["kind"], kind)
                self.assertEqual(res["phase"], phase)
                self.assertFalse(res["read_only"])
                self.assertIsNone(res["report_path"])
            feat_task = _task(feat["task_id"])
            self.assertEqual(feat_task.title, "[define] Add caching")
            self.assertIn("## Factonio Definition Card (feature definition)", feat_task.body)
            self.assertNotIn("Contract", feat_task.body)
            bug_task = _task(bug["task_id"])
            self.assertIn("## Factonio Definition Card (bug diagnosis)", bug_task.body)
            quick_task = _task(quick["task_id"])
            self.assertEqual(quick_task.title, "[implement] Fix typo")
            self.assertEqual(quick_task.skills, ["factonio:implement"])

    def test_unknown_kind_rejected_before_any_card(self):
        from factonio.orchestrator import start_workstream

        with isolated_hermes_home() as home:
            _register(home)
            with self.assertRaises(ValueError) as ctx:
                start_workstream("acme/sample", "Do it", kind="chore")
            self.assertIn("investigate", str(ctx.exception))
            self.assertEqual(_task_count(), 0)

    def test_kind_is_canonicalized(self):
        from factonio.orchestrator import start_workstream

        with isolated_hermes_home() as home:
            _register(home)
            res = start_workstream("acme/sample", "Speed up parse", kind="  PERF ", issue_number=4)
            self.assertEqual(res["kind"], "perf")

    def test_refactor_embeds_behavior_preservation_pins(self):
        from factonio.orchestrator import start_workstream

        with isolated_hermes_home() as home:
            _register(home)
            res = start_workstream("acme/sample", "Split repos.py into modules", kind="refactor", issue_number=7)
            self.assertEqual((res["kind"], res["phase"], res["read_only"], res["report_path"]),
                             ("refactor", "define", False, None))
            task = _task(res["task_id"])
            self.assertEqual(task.title, "[define] Split repos.py into modules")
            self.assertEqual(task.skills, ["factonio:define"])
            body = task.body
            self.assertIn("- Mode: `refactor`", body)
            self.assertIn("## Refactor Contract (behavior preservation)", body)
            self.assertIn("characterization tests", body)
            self.assertIn("BEFORE any internal edit", body)
            self.assertIn("zero unintended behavioral drift", body)
            self.assertIn(res["spec_path"], body)

    def test_perf_embeds_seven_questions_and_seven_mantras(self):
        from factonio.orchestrator import (
            BENCHMARK_MEASUREMENT_MANTRAS,
            BENCHMARK_PRE_OPT_QUESTIONS,
            start_workstream,
        )

        with isolated_hermes_home() as home:
            _register(home)
            res = start_workstream("acme/sample", "Cut p95 of /v1/events", kind="perf", issue_number=8)
            self.assertEqual(res["kind"], "perf")
            self.assertEqual(res["phase"], "define")
            body = _task(res["task_id"]).body
            self.assertIn("## Performance Contract (benchmark-checklist)", body)
            self.assertIn("### 7 Pre-Optimization Questions", body)
            self.assertIn("### 7 Measurement Mantras", body)
            for i, q in enumerate(BENCHMARK_PRE_OPT_QUESTIONS, 1):
                self.assertIn(f"{i}. {q}", body)
            for i, m in enumerate(BENCHMARK_MEASUREMENT_MANTRAS, 1):
                self.assertIn(f"{i}. {m}", body)
            self.assertLess(body.index("### 7 Pre-Optimization Questions"), body.index("### 7 Measurement Mantras"))

    def test_refactor_and_perf_skip_define_carry_contract_on_implement_card(self):
        from factonio.orchestrator import start_workstream

        with isolated_hermes_home() as home:
            _register(home)
            ref = start_workstream("acme/sample", "Rename helper", kind="refactor", skip_define=True, issue_number=10)
            perf = start_workstream("acme/sample", "Cache regex", kind="perf", skip_define=True, issue_number=11)
            self.assertEqual((ref["phase"], perf["phase"]), ("implement", "implement"))
            ref_task, perf_task = _task(ref["task_id"]), _task(perf["task_id"])
            self.assertTrue(ref_task.title.startswith("[implement] "))
            self.assertIn("## Refactor Contract (behavior preservation)", ref_task.body)
            self.assertIn("## Performance Contract (benchmark-checklist)", perf_task.body)

    def test_investigate_is_read_only_define_card(self):
        from factonio.metrics import classify_phase
        from factonio.orchestrator import investigation_report_path, start_workstream

        with isolated_hermes_home() as home:
            _register(home)
            res = start_workstream(
                "acme/sample", "Why do webhooks double-fire?\nSeen in prod.", kind="investigate", issue_number=12,
            )
            self.assertTrue(res["ok"])
            self.assertEqual(res["kind"], "investigate")
            self.assertEqual(res["phase"], "define")
            self.assertEqual(res["skill"], "factonio:define")
            self.assertTrue(res["read_only"])
            self.assertEqual(res["report_path"], str(investigation_report_path(res["tenant"])))
            self.assertTrue(res["report_path"].endswith("investigation.md"))
            self.assertEqual(res["completion_contract"], "local-only")

            task = _task(res["task_id"])
            self.assertEqual(task.title, "[define] [investigate] Why do webhooks double-fire?")
            self.assertEqual(classify_phase({"title": task.title, "skills": json.dumps(task.skills)}), "define")
            self.assertEqual(classify_phase({"title": task.title}), "define")
            body = task.body
            self.assertIn("read-only investigation", body)
            self.assertIn("## Investigation Contract (read-only)", body)
            self.assertIn(res["report_path"], body)
            self.assertIn("do NOT commit, push, create branches, or open pull requests", body)
            self.assertIn("Speculative fixes are forbidden", body)
            self.assertNotIn(res["holdout_dir"], body)
            self.assertNotIn("Task DAG", body.split("## Investigation Contract", 1)[0])

    def test_investigate_rejects_skip_define_without_creating_cards(self):
        from factonio import trail
        from factonio.orchestrator import start_workstream

        with isolated_hermes_home() as home:
            _register(home)
            with self.assertRaises(ValueError) as ctx:
                start_workstream("acme/sample", "Look into it", kind="investigate", skip_define=True)
            self.assertIn("skip_define", str(ctx.exception))
            self.assertEqual(_task_count(), 0)
            self.assertEqual(trail.query_decisions(repo="acme-sample"), [])

    def test_investigate_trail_records_read_only(self):
        from factonio import trail
        from factonio.orchestrator import start_workstream

        with isolated_hermes_home() as home:
            _register(home)
            res = start_workstream("acme/sample", "Root-cause flaky CI", kind="investigate", issue_number=13)
            rows = trail.query_decisions(tenant=res["tenant"])
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["decision"], "start_workstream")
            self.assertEqual(rows[0]["phase"], "define")
            self.assertEqual(rows[0]["evidence"]["kind"], "investigate")
            self.assertTrue(rows[0]["evidence"]["read_only"])


class TestStartToolSchema(unittest.TestCase):
    def _tools(self) -> dict[str, Any]:
        from factonio.agent_tools import register_tools

        ctx = _FakeCtx()
        register_tools(ctx)
        return ctx.tools

    def test_kind_enum_matches_workstream_kinds(self):
        from factonio.orchestrator import WORKSTREAM_KINDS

        with isolated_hermes_home():
            schema = self._tools()["factonio_start"]["schema"]
        kind = schema["parameters"]["properties"]["kind"]
        self.assertEqual(kind["enum"], list(WORKSTREAM_KINDS))
        self.assertEqual(kind["default"], "feature")

    def test_tool_starts_investigate_and_reports_errors(self):
        with isolated_hermes_home() as home:
            _register(home)
            handler = self._tools()["factonio_start"]["handler"]
            ok = json.loads(handler({"repo": "acme/sample", "request": "Trace leak", "kind": "investigate",
                                     "issue_number": 21}))
            self.assertTrue(ok["ok"])
            self.assertTrue(ok["read_only"])
            self.assertTrue(ok["report_path"].endswith("investigation.md"))
            err = json.loads(handler({"repo": "acme/sample", "request": "x", "kind": "investigate",
                                      "skip_define": True}))
            self.assertFalse(err["ok"])
            self.assertIn("skip_define", err["error"])
            bad = json.loads(handler({"repo": "acme/sample", "request": "x", "kind": "nope"}))
            self.assertFalse(bad["ok"])


class TestStartCli(unittest.TestCase):
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

    def test_start_json_for_every_kind(self):
        from factonio.orchestrator import WORKSTREAM_KINDS

        with isolated_hermes_home() as home:
            _register(home)
            run = self._cli()
            for n, kind in enumerate(WORKSTREAM_KINDS, 1):
                code, out, err = run("start", "acme/sample", f"Request {kind}", "--kind", kind,
                                     "--issue", str(100 + n), "--json")
                self.assertEqual(code, 0, err)
                res = json.loads(out)
                self.assertEqual(res["kind"], kind)
                self.assertEqual(res["tenant"], f"acme-sample-i{100 + n}")
                self.assertEqual(res["read_only"], kind == "investigate")
                self.assertEqual(res["phase"], "implement" if kind == "quick" else "define")
            self.assertEqual(_task_count(), len(WORKSTREAM_KINDS))

    def test_start_flags_title_skip_define_and_escape(self):
        with isolated_hermes_home() as home:
            _register(home)
            run = self._cli()
            code, out, _ = run("--json", "start", "acme/sample", "Long request text", "--title", "Short",
                               "--kind", "bug", "--skip-define", "--escaped-from", "t_verify1", "--issue", "9")
            self.assertEqual(code, 0)
            res = json.loads(out)
            self.assertEqual(res["phase"], "implement")
            self.assertEqual(_task(res["task_id"]).title, "[escape] [implement] Short")

    def test_start_text_output(self):
        with isolated_hermes_home() as home:
            _register(home)
            run = self._cli()
            code, out, _ = run("start", "acme/sample", "Why slow?", "--kind", "investigate", "--issue", "5")
            self.assertEqual(code, 0)
            self.assertIn("Started investigate workstream acme-sample-i5", out)
            self.assertIn("investigation.md", out)

    def test_start_errors_exit_nonzero(self):
        with isolated_hermes_home() as home:
            _register(home)
            run = self._cli()
            code, out, _ = run("start", "acme/sample", "x", "--kind", "investigate", "--skip-define", "--json")
            self.assertEqual(code, 1)
            self.assertFalse(json.loads(out)["ok"])
            code, _, err = run("start", "acme/sample", "x", "--kind", "investigate", "--skip-define")
            self.assertEqual(code, 1)
            self.assertIn("skip_define", err)
            code, out, _ = run("start", "acme/missing", "x", "--json")
            self.assertEqual(code, 1)
            self.assertIn("not registered", json.loads(out)["error"])
            code, _, _ = run("start", "acme/sample", "x", "--kind", "chore")
            self.assertEqual(code, 2)
            self.assertEqual(_task_count(), 0)

    def test_usage_mentions_start(self):
        with isolated_hermes_home():
            code, out, _ = self._cli()()
        self.assertEqual(code, 0)
        self.assertIn("start", out)


class TestModeSkillProse(unittest.TestCase):
    @staticmethod
    def _read(rel: str) -> str:
        return (PLUGIN_ROOT / rel).read_text(encoding="utf-8")

    def test_define_section_1c_order_and_content(self):
        text = self._read("skills/define/SKILL.md")
        heading = "### 1c. Mode-Specific Contracts (refactor, perf, investigate)"
        self.assertIn(heading, text)
        self.assertLess(text.index("### 1b. Architect"), text.index(heading))
        self.assertLess(text.index(heading), text.index("### 2. Write `spec.md`"))
        section = text.split(heading, 1)[1].split("### 2.", 1)[0]
        for needle in ("`refactor`", "`perf`", "`investigate`", "7 pre-optimization questions",
                       "7 measurement mantras", "investigation.md", "open pull requests"):
            self.assertIn(needle, section)

    def test_verify_diff_mode_checks_inside_checklist(self):
        text = self._read("skills/verify-diff/SKILL.md")
        checklist = text.split("## Review Checklist", 1)[1].split("\n## ", 1)[0]
        self.assertIn("11. **Mode-specific checks", checklist)
        self.assertLess(checklist.index("10. **Gate-Integrity Audit"), checklist.index("11. **Mode-specific checks"))
        mode = checklist.split("11. **Mode-specific checks", 1)[1]
        for needle in ("`refactor`", "zero unintended behavioral drift", "`perf`", "identical inputs", "warmup",
                       "variance", "correctness regression"):
            self.assertIn(needle, mode)

    def test_orchestrator_section_1_lists_modes_without_touching_merge_gate(self):
        text = self._read("skills/orchestrator/SKILL.md")
        section1 = text.split("### 1. Intake and Clarification", 1)[1].split("### 2.", 1)[0]
        for needle in ('kind="refactor"', 'kind="perf"', 'kind="investigate"', "`benchmark-checklist`",
                       "`report_path`", "`skip_define` is rejected", "hermes factonio start"):
            self.assertIn(needle, section1)
        gate = text.split("### 5. Merge Gate", 1)[1].split("## Decision Trail", 1)[0]
        self.assertNotIn("investigate", gate)

    def test_orchestrator_prompt_section_stays_small(self):
        import factonio

        self.assertLess(len(factonio.ORCHESTRATOR_PROMPT_SECTION), 2900)


if __name__ == "__main__":
    unittest.main()
