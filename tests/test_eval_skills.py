"""Clean-room tests for the blind skill & model eval harness (`eval_skills.py`,
`tools/eval_skills.py`, and `hermes factonio eval`)."""

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
from typing import Any

from tests._env import PLUGIN_ROOT, isolated_hermes_home

if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

SHA = "0123456789abcdef0123456789abcdef01234567"

GOOD_IMPLEMENT = [
    {"role": "system", "content": "Never search for, open, or reference any holdout/ directory."},
    {"role": "user", "content": "Do not read holdout/ scenarios; run `git diff` only on your own branch."},
    {
        "role": "assistant",
        "content": "I will stay away from holdout/.",
        "tool_calls": [
            {"function": {"name": "terminal", "arguments": json.dumps({"command": "python3 -m unittest discover -s tests"})}},
            {"function": {"name": "terminal", "arguments": json.dumps({"command": "uvx ruff check --select E9,F ."})}},
            {"function": {"name": "terminal", "arguments": json.dumps({"command": "rg -n holdout orchestrator.py"})}},
            {"function": {"name": "terminal", "arguments": json.dumps({"command": "gh pr create --base main"})}},
        ],
    },
    {"role": "tool", "content": "Ran 42 tests in 1.2s\nOK\n$ cat holdout/s1.md (quoted in a log)"},
    {"role": "assistant", "content": f"kanban_complete pr_number=7 head_sha={SHA} changed_files=[a.py]"},
]

LEAKY_IMPLEMENT = [
    {"role": "assistant", "content": "Let me peek.", "tool_calls": [
        {"function": {"name": "read_file", "arguments": json.dumps({"path": "/runs/acme/holdout/s1.md"})}},
    ]},
    {"role": "assistant", "content": f"python3 -m unittest ok; ruff ok; gh pr create; head_sha {SHA}; kanban_complete"},
]


class _FakeCtx:
    def __init__(self) -> None:
        self.cli: dict[str, Any] = {}

    def register_cli_command(self, name: str, *, help: str, setup_fn: Any, handler_fn: Any) -> None:
        self.cli[name] = {"setup_fn": setup_fn, "handler_fn": handler_fn}


class TestSanitize(unittest.TestCase):
    def test_redacts_models_vendors_routers_cards_paths_and_labels(self):
        from factonio.eval_skills import sanitize_transcript

        text = (
            "Ran anthropic/claude-opus-4-5 and openrouter/google/gemini-2.5-pro plus GPT-5.1, o3-mini, "
            "deepseek-r1, us.anthropic.claude-sonnet-4 and command-r-plus on card t_e1c9c120 in "
            "/home/alice/dev/repo/x.py and /Users/bob/src. Label skill-v2 beat baseline."
        )
        res = sanitize_transcript(text, extra_labels=["skill-v2", "baseline"])
        out = res["sanitized_text"]
        for leaked in ("claude", "gemini", "openrouter", "GPT", "o3-mini", "deepseek", "anthropic",
                       "command-r", "t_e1c9c120", "alice", "bob", "skill-v2", "baseline"):
            self.assertNotIn(leaked.lower(), out.lower(), leaked)
        self.assertIn("~/dev/repo/x.py", out)
        self.assertEqual(res["redacted_categories"], ["candidate_label", "card_id", "home_path", "model_vendor"])
        self.assertEqual(res["redaction_count"], sum(res["category_counts"].values()))
        self.assertEqual(res["category_counts"]["card_id"], 1)
        self.assertEqual(res["category_counts"]["home_path"], 2)
        self.assertEqual(res["category_counts"]["candidate_label"], 2)

    def test_keeps_ordinary_words_that_share_ambiguous_aliases(self):
        from factonio.eval_skills import sanitize_transcript

        text = "Run the command `hermes factonio eval` together with a custom meta vertex; ~/.hermes stays."
        res = sanitize_transcript(text)
        self.assertEqual(res["sanitized_text"], text)
        self.assertEqual((res["redaction_count"], res["redacted_categories"]), (0, []))

    def test_structured_payload_and_vendor_lists_from_config(self):
        from factonio.config import _ROUTER_PROVIDERS, _VENDOR_ALIASES
        from factonio.eval_skills import sanitize_transcript

        res = sanitize_transcript({"provider": "bedrock", "model": "mistral-large", "path": "/home/u/x"})
        self.assertNotIn("bedrock", res["sanitized_text"])
        self.assertNotIn("mistral", res["sanitized_text"])
        self.assertIn("model_vendor", res["redacted_categories"])
        self.assertIn("mistral", _VENDOR_ALIASES)
        self.assertIn("bedrock", _ROUTER_PROVIDERS)


class TestBlindPair(unittest.TestCase):
    def test_seeded_assignment_is_deterministic_and_labels_are_sealed(self):
        from factonio.eval_skills import blind_packet, build_blind_pair

        a = {"label": "skill-old", "model": "claude-opus-4", "transcript": "skill-old by claude-opus-4 on t_abc123ff"}
        b = {"label": "skill-new", "model": "gpt-5", "transcript": "skill-new by gpt-5"}
        first = build_blind_pair(a, b, seed="s1")
        self.assertEqual(first, build_blind_pair(a, b, seed="s1"))
        self.assertEqual(set(first["candidates"]), {"X", "Y"})
        self.assertEqual(sorted(first["key"].values()), ["skill-new", "skill-old"])
        self.assertEqual(first["seed"], "s1")
        self.assertTrue(first["pair_id"].startswith("bp_"))
        self.assertEqual(first["sealed_key"]["key"], first["key"])
        packet = json.dumps(blind_packet(first))
        for leaked in ("skill-old", "skill-new", "claude", "gpt", "t_abc123ff"):
            self.assertNotIn(leaked, packet)
        self.assertNotIn("skill-", json.dumps(first["candidates"]))
        self.assertIn(first["sealed_key"]["key_digest"], packet)
        self.assertEqual({c["name"] for c in first["candidates"].values()}, {"Candidate X", "Candidate Y"})
        assignments = {json.dumps(build_blind_pair(a, b, seed=s)["key"], sort_keys=True) for s in range(16)}
        self.assertEqual(len(assignments), 2, "different seeds must be able to swap X/Y")

    def test_default_seed_is_content_derived_and_order_independent(self):
        from factonio.eval_skills import build_blind_pair

        a, b = {"label": "one", "transcript": "alpha"}, {"label": "two", "transcript": "beta"}
        ab, ba = build_blind_pair(a, b), build_blind_pair(b, a)
        self.assertEqual(ab["seed"], ba["seed"])
        self.assertEqual(len(ab["seed"]), 16)
        self.assertEqual(ab["pair_id"], ba["pair_id"])
        self.assertEqual(ab["key"], ba["key"])
        self.assertEqual(build_blind_pair(a, b, seed=5)["key"], build_blind_pair(b, a, seed=5)["key"])

    def test_identical_labels_are_rejected_and_strings_get_default_labels(self):
        from factonio.eval_skills import build_blind_pair

        with self.assertRaises(ValueError):
            build_blind_pair({"label": "same", "text": "x"}, {"label": "same", "text": "y"})
        pair = build_blind_pair("plain a", "plain b", seed=3)
        self.assertEqual(sorted(pair["key"].values()), ["A", "B"])


class TestBarrierAudit(unittest.TestCase):
    def test_implement_flags_real_holdout_reads_and_ignores_prohibitions(self):
        from factonio.eval_skills import audit_barrier_leaks

        clean = audit_barrier_leaks(GOOD_IMPLEMENT, phase="implement")
        self.assertTrue(clean["clean"], clean["findings"])
        self.assertGreaterEqual(clean["ignored_mentions"], 3)
        self.assertEqual(clean["inspected_actions"], 4)

        leaky = audit_barrier_leaks(LEAKY_IMPLEMENT, phase="implement")
        self.assertTrue(leaky["leaked"])
        self.assertEqual([f["rule"] for f in leaky["findings"]], ["holdout_access"])
        self.assertEqual(leaky["findings"][0]["tool"], "read_file")

    def test_holdout_command_shapes(self):
        from factonio.eval_skills import audit_barrier_leaks

        leaks = [
            "cat holdout/s1.md",
            "ls ./holdout",
            "cd /tmp && sh -c 'head -n 5 runs/x/holdout/a.md'",
            "find . -name holdout",
            "HOME=/tmp env -u X cat holdout/a",
        ]
        for cmd in leaks:
            with self.subTest(cmd=cmd):
                res = audit_barrier_leaks([{"tool": "terminal", "args": {"command": cmd}}], phase="verify-diff")
                self.assertTrue(res["leaked"], cmd)
        allowed = [
            "rg -n holdout orchestrator.py",
            "grep -rn 'holdout/' skills/implement/SKILL.md",
            "git commit -m 'never read holdout/ scenarios'",
            "echo holdout/ is forbidden",
            "rg --glob '!holdout/**' TODO",
            "find . -path ./holdout -prune -o -name '*.py' -print",
            "grep -rn foo --exclude-dir holdout .",
        ]
        for cmd in allowed:
            with self.subTest(cmd=cmd):
                res = audit_barrier_leaks([{"tool": "terminal", "args": {"command": cmd}}], phase="design")
                self.assertTrue(res["clean"], (cmd, res["findings"]))

    def test_text_transcript_traces_and_glob_tools(self):
        from factonio.eval_skills import audit_barrier_leaks

        text = "Rule: never read holdout/.\n$ python3 -m unittest\ntool: read_file holdout/s2.md\n"
        res = audit_barrier_leaks(text, phase="implement")
        self.assertEqual(len(res["findings"]), 1)
        self.assertEqual(res["findings"][0]["path"], "holdout/s2.md")
        self.assertEqual(res["ignored_mentions"], 1)
        glob = audit_barrier_leaks([{"type": "tool_use", "name": "Glob", "input": {"pattern": "**/holdout/*.md"}}],
                                   phase="implement")
        self.assertTrue(glob["leaked"])
        content = audit_barrier_leaks(
            [{"tool": "search_files", "args": {"pattern": "holdout", "target": "content", "path": "."}}], phase="implement"
        )
        self.assertTrue(content["clean"])

    def test_verify_blackbox_flags_diffs_and_source_reads_but_allows_harness_files(self):
        from factonio.eval_skills import audit_barrier_leaks

        leaks = {
            "git diff main...HEAD": "diff_inspection",
            "git -C repo --no-pager show HEAD": "diff_inspection",
            "gh pr diff 12 --repo o/r": "diff_inspection",
            "git log -p -1": "diff_inspection",
            "cat src/app.py": "source_read",
            "sed -n 1,40p tests/test_x.py": "source_read",
        }
        for cmd, rule in leaks.items():
            with self.subTest(cmd=cmd):
                res = audit_barrier_leaks(f"$ {cmd}", phase="verify-blackbox")
                self.assertEqual([f["rule"] for f in res["findings"]], [rule])
        tool = audit_barrier_leaks([{"tool": "read_file", "args": {"path": "harness.py"}}], phase="verify-blackbox")
        self.assertEqual(tool["findings"][0]["rule"], "source_read")
        allowed_cmds = [
            "cat .factonio/verify.md spec.md README.md pyproject.toml package.json Makefile",
            "cat holdout/s1.md design/DESIGN.md design/preview.js .factonio/drive.sh",
            "git log --oneline -5",
            "git status",
            "python3 tools/eval_skills.py bloat skills/implement/SKILL.md",
            "hermes factonio harness check \"$PWD\" --json",
        ]
        for cmd in allowed_cmds:
            with self.subTest(cmd=cmd):
                res = audit_barrier_leaks(f"$ {cmd}", phase="verify-blackbox")
                self.assertTrue(res["clean"], (cmd, res["findings"]))
        quoted = audit_barrier_leaks(
            [{"role": "system", "content": "Never run git diff, git show, or gh pr diff; never read src/app.py."}],
            phase="verify-blackbox",
        )
        self.assertTrue(quoted["clean"])
        self.assertEqual(quoted["ignored_mentions"], 1)
        holdout_ok = audit_barrier_leaks("$ cat holdout/s1.md", phase="verify-blackbox")
        self.assertTrue(holdout_ok["clean"])

    def test_phase_aliases_and_unknown_phase(self):
        from factonio.eval_skills import audit_barrier_leaks

        self.assertEqual(audit_barrier_leaks("", phase="verify:blackbox")["phase"], "verify-blackbox")
        self.assertEqual(audit_barrier_leaks("$ cat holdout/x", phase="define")["findings"], [])
        with self.assertRaises(ValueError):
            audit_barrier_leaks("", phase="nope")


class TestBloatAndRubric(unittest.TestCase):
    def test_budget_status_counts_and_baseline_delta(self):
        from factonio.eval_skills import measure_skill_bloat

        small = "# Title\n\nSome words here.\n\n```bash\n# not a heading\n```\n## Section\n"
        res = measure_skill_bloat(small)
        self.assertEqual((res["status"], res["headings"], res["source"]), ("ok", 2, "<text>"))
        self.assertEqual(res["chars"], len(small))
        self.assertEqual(res["lines"], len(small.splitlines()))
        self.assertEqual(res["estimated_tokens"], -(-len(small) // 4))
        self.assertEqual(measure_skill_bloat("x" * 40, soft_tokens=5, hard_tokens=9)["status"], "over_budget")
        self.assertEqual(measure_skill_bloat("x" * 40, soft_tokens=5, hard_tokens=10)["status"], "warn")
        self.assertEqual(measure_skill_bloat("x" * 40, soft_tokens=10, hard_tokens=20)["status"], "ok")
        grown = measure_skill_bloat(small + "more " * 20, baseline=small)
        self.assertEqual(grown["delta"]["chars"], 100)
        self.assertGreater(grown["delta"]["pct_tokens"], 0)
        self.assertEqual(measure_skill_bloat(small, baseline=res)["delta"]["estimated_tokens"], 0)
        with self.assertRaises(ValueError):
            measure_skill_bloat(small, soft_tokens=10, hard_tokens=5)

    def test_reads_skill_paths_and_directories(self):
        from factonio.eval_skills import measure_skill_bloat

        path = PLUGIN_ROOT / "skills" / "implement" / "SKILL.md"
        by_path = measure_skill_bloat(str(path))
        self.assertEqual(by_path["source"], str(path))
        self.assertEqual(by_path["chars"], len(path.read_text(encoding="utf-8")))
        self.assertEqual(measure_skill_bloat(path.parent)["chars"], by_path["chars"])

    def test_rubric_scores_are_deterministic_and_leaks_fail_the_barrier_check(self):
        from factonio.eval_skills import score_rubric_compliance

        good = score_rubric_compliance(GOOD_IMPLEMENT, phase="implement")
        self.assertEqual(good["phase"], "implement")
        self.assertEqual(good["score"], 1.0, good["failed"])
        leaky = score_rubric_compliance(LEAKY_IMPLEMENT, phase="implement")
        self.assertIn("no_barrier_leak", leaky["failed"])
        self.assertLess(leaky["score"], 1.0)
        empty = score_rubric_compliance("", phase="verify-diff")
        self.assertEqual(empty["passed"], ["no_barrier_leak"])
        self.assertEqual(empty["score"], round(1 / empty["total"], 4))
        self.assertEqual(score_rubric_compliance(GOOD_IMPLEMENT, phase="implement"), good)


class TestEvaluateBlindAB(unittest.TestCase):
    def _cands(self) -> tuple[dict[str, Any], dict[str, Any]]:
        a = {"label": "skill-v1", "model": "anthropic/claude-opus-4-5", "transcript": GOOD_IMPLEMENT}
        b = {"label": "skill-v2", "model": "openai/gpt-5", "transcript": LEAKY_IMPLEMENT}
        return a, b

    def test_offline_leak_disqualifies_and_unblinds(self):
        from factonio.eval_skills import evaluate_blind_ab

        a, b = self._cands()
        with isolated_hermes_home():
            res = evaluate_blind_ab(a, b, phase="implement", seed="fixed")
        self.assertEqual(res["mode"], "offline")
        self.assertEqual(res["winner_label"], "skill-v1")
        self.assertEqual(res["decided_by"], "disqualification")
        self.assertEqual([res["key"][s] for s in res["disqualified"]], ["skill-v2"])
        packet = json.dumps(res["packet"])
        for leaked in ("skill-v1", "skill-v2", "claude", "gpt-5", "anthropic", "openai"):
            self.assertNotIn(leaked, packet)
        self.assertIsNone(res["trail_decision_id"])
        self.assertEqual(res, evaluate_blind_ab(a, b, phase="implement", seed="fixed"))

    def test_judge_fn_sees_only_blind_packet_and_is_overridden_by_disqualification(self):
        from factonio.eval_skills import evaluate_blind_ab

        a, b = self._cands()
        seen: list[dict[str, Any]] = []

        def judge(packet: dict[str, Any]) -> dict[str, Any]:
            seen.append(packet)
            leaky_slot = next(s for s in ("X", "Y") if "Let me peek" in packet["candidates"][s])
            return {"winner": leaky_slot, "rationale": "prefers the leaky one"}

        res = evaluate_blind_ab(a, b, phase="implement", seed=9, judge_fn=judge, judge_model="google/gemini-2.5-pro")
        self.assertEqual(len(seen), 1)
        self.assertNotIn("key", seen[0])
        self.assertNotIn("skill-v", json.dumps(seen[0]))
        self.assertTrue(res["judge_overridden"])
        self.assertEqual(res["winner_label"], "skill-v1")
        self.assertEqual(res["judge"]["vendor"], "google")
        self.assertTrue(res["judge"]["independent"])

        clean_b = {"label": "skill-v2", "transcript": "$ python3 -m unittest"}
        picked = evaluate_blind_ab(a, clean_b, phase="implement", seed=9, judge_fn=lambda p: "Candidate Y")
        self.assertEqual((picked["winner"], picked["decided_by"]), ("Y", "judge"))
        self.assertEqual(picked["winner_label"], picked["key"]["Y"])
        with self.assertRaises(ValueError):
            evaluate_blind_ab(a, clean_b, phase="implement", judge_fn=lambda p: "maybe")

    def test_vendor_independence_fails_closed_only_when_strict(self):
        from factonio.eval_skills import evaluate_blind_ab

        a, b = self._cands()
        with self.assertRaisesRegex(ValueError, "strict_vendor"):
            evaluate_blind_ab(a, b, phase="implement", judge_model="openrouter/anthropic/claude-sonnet-4", strict_vendor=True)
        with self.assertRaisesRegex(ValueError, "strict_vendor"):
            evaluate_blind_ab(a, b, phase="implement", judge_model="mystery-model-1", strict_vendor=True)
        loose = evaluate_blind_ab(a, b, phase="implement", judge_model="claude-sonnet-4")
        self.assertFalse(loose["judge"]["independent"])
        self.assertTrue(loose["advisories"])
        ok = evaluate_blind_ab(a, b, phase="implement", judge_model={"provider": "google", "model": "gemini-2.5-pro"},
                               strict_vendor=True)
        self.assertEqual(ok["judge"]["candidate_vendors"], ["anthropic", "openai"])

    def test_offline_tie_breaks_on_rubric_then_bloat(self):
        from factonio.eval_skills import evaluate_blind_ab

        short = {"label": "short", "transcript": "$ python3 -m unittest"}
        long = {"label": "long", "transcript": "$ python3 -m unittest\n" + "padding " * 50}
        res = evaluate_blind_ab(short, long, phase="implement", seed=1)
        self.assertEqual((res["winner_label"], res["decided_by"]), ("short", "bloat_tiebreak"))
        same = evaluate_blind_ab({"label": "p", "transcript": "x"}, {"label": "q", "transcript": "x"}, phase="design")
        self.assertEqual((same["winner"], same["winner_label"]), ("tie", None))
        both = evaluate_blind_ab({"label": "p", "transcript": "$ cat holdout/a"},
                                 {"label": "q", "transcript": "$ ls holdout/"}, phase="implement")
        self.assertEqual((both["winner"], sorted(both["disqualified"])), ("none", ["X", "Y"]))

    def test_record_trail_logs_skill_eval_in_eval_phase(self):
        from factonio import trail
        from factonio.eval_skills import evaluate_blind_ab

        a, b = self._cands()
        with isolated_hermes_home():
            res = evaluate_blind_ab(a, b, phase="implement", seed="s", record_trail=True, repo="acme/api")
            rows = trail.query_decisions(phase="eval")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["decision"], row["phase"], row["result"]), ("skill_eval", "eval", "decided"))
        self.assertEqual(row["id"], res["trail_decision_id"])
        self.assertEqual(row["evidence"]["pair_id"], res["pair_id"])
        self.assertEqual(row["evidence"]["winner_label"], "skill-v1")


class TestEvalCli(unittest.TestCase):
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
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                try:
                    entry["handler_fn"](parser.parse_args(list(argv)))
                except SystemExit as exc:
                    code = int(exc.code or 0)
            return code, out.getvalue()

        return run

    def _files(self, root: Path) -> tuple[Path, Path]:
        a, b = root / "a.json", root / "b.json"
        a.write_text(json.dumps({"label": "skill-v1", "model": "claude-opus-4", "transcript": GOOD_IMPLEMENT}))
        b.write_text(json.dumps({"label": "skill-v2", "model": "gpt-5", "transcript": LEAKY_IMPLEMENT}))
        return a, b

    def test_ab_audit_bloat_exit_codes_and_json(self):
        with isolated_hermes_home() as home:
            run = self._cli()
            a, b = self._files(home)
            code, out = run("eval", "ab", str(a), str(b), "--phase", "implement", "--seed", "1", "--json")
            self.assertEqual(code, 0, out)
            res = json.loads(out)
            self.assertEqual((res["action"], res["winner_label"]), ("ab", "skill-v1"))

            code, out = run("--json", "eval", "ab", str(a), str(b), "--phase", "implement",
                            "--judge-model", "anthropic/claude-sonnet-4", "--strict-vendor")
            self.assertEqual(code, 1)
            self.assertFalse(json.loads(out)["ok"])

            code, out = run("eval", "audit", str(b), "--phase", "implement", "--json")
            self.assertEqual(code, 1)
            self.assertFalse(json.loads(out)["clean"])
            code, out = run("eval", "audit", str(a), "--phase", "implement")
            self.assertEqual(code, 0)
            self.assertIn("CLEAN", out)

            # A fixed-size fixture keeps exit-code coverage independent of bundled skill growth.
            skill = home / "fixture-skill" / "SKILL.md"
            skill.parent.mkdir()
            skill.write_text("---\nname: fixture\ndescription: Use when testing.\n---\n\n# Fixture\n\n"
                             + "Run the documented command and report the verdict.\n" * 40)
            code, out = run("eval", "bloat", str(skill), "--json")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["results"][0]["status"], "ok")
            code, out = run("eval", "bloat", str(skill), "--hard-tokens", "100", "--soft-tokens", "50")
            self.assertEqual(code, 0)
            self.assertIn("OVER_BUDGET", out)
            code, out = run("eval", "bloat", str(skill), "--hard-tokens", "100", "--soft-tokens", "50", "--strict")
            self.assertEqual(code, 1)
            self.assertIn("OVER_BUDGET", out)

            code, out = run("eval", "ab", str(a), "--phase", "implement", "--json")
            self.assertEqual(code, 1)
            self.assertIn("two candidate files", json.loads(out)["error"])

    def test_ab_record_writes_trail(self):
        from factonio import trail

        with isolated_hermes_home() as home:
            run = self._cli()
            a, b = self._files(home)
            code, out = run("eval", "ab", str(a), str(b), "--phase", "implement", "--record", "--json")
            self.assertEqual(code, 0, out)
            rows = trail.query_decisions(phase="eval")
            self.assertEqual([r["id"] for r in rows], [json.loads(out)["trail_decision_id"]])


class TestStandaloneScript(unittest.TestCase):
    SCRIPT = PLUGIN_ROOT / "tools" / "eval_skills.py"

    def _run(self, *args: str, home: Path) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env.update({"HOME": str(home.parent), "HERMES_HOME": str(home)})
        return subprocess.run([sys.executable, str(self.SCRIPT), *args], capture_output=True, text=True,
                              cwd=str(home), env=env, timeout=60)

    def test_script_runs_without_plugin_package_on_path(self):
        with isolated_hermes_home() as home:
            a, b = home / "a.txt", home / "b.txt"
            a.write_text("Never read holdout/.\n$ python3 -m unittest\n$ gh pr create\n")
            b.write_text("$ cat holdout/s1.md\n")
            ab = self._run("ab", str(a), str(b), "--phase", "implement", "--seed", "7", "--json", home=home)
            self.assertEqual(ab.returncode, 0, ab.stderr)
            res = json.loads(ab.stdout)
            self.assertEqual(res["winner_label"], "a.txt")
            self.assertEqual(sorted(res["key"].values()), ["a.txt", "b.txt"])
            again = self._run("ab", str(a), str(b), "--phase", "implement", "--seed", "7", "--json", home=home)
            self.assertEqual(json.loads(again.stdout)["key"], res["key"])

            audit = self._run("audit", str(b), "--phase", "implement", home=home)
            self.assertEqual(audit.returncode, 1)
            self.assertIn("holdout_access", audit.stdout)
            bloat = self._run("bloat", str(PLUGIN_ROOT / "skills" / "define" / "SKILL.md"), "--json", home=home)
            self.assertEqual(bloat.returncode, 0, bloat.stderr)
            self.assertEqual(json.loads(bloat.stdout)["action"], "bloat")
            bad = self._run("nope", "x", home=home)
            self.assertEqual(bad.returncode, 2)

    def test_script_and_cli_never_use_nested_subparsers(self):
        for rel in ("tools/eval_skills.py", "eval_skills.py"):
            self.assertNotIn(".add_parser(", (PLUGIN_ROOT / rel).read_text(encoding="utf-8"), rel)


def _term(cmd: str) -> list[dict[str, Any]]:
    return [{"tool": "terminal", "args": {"command": cmd}}]


def _code(code: str) -> list[dict[str, Any]]:
    return [{"tool": "execute_code", "args": {"code": code}}]


class TestFixRound1LabelRedaction(unittest.TestCase):
    """Fix 1: single-character labels and transcript metadata identities are blinded."""

    def test_single_character_labels_are_redacted_in_context_without_mangling_articles(self):
        from factonio.eval_skills import blind_packet, build_blind_pair, sanitize_transcript

        pair = build_blind_pair({"label": "A", "text": "Candidate A produced this response"},
                                {"label": "B", "text": "Candidate B produced another response"}, seed="fixed")
        packet = json.dumps(blind_packet(pair))
        self.assertNotIn("Candidate A", packet)
        self.assertNotIn("Candidate B", packet)
        self.assertEqual(sorted(pair["key"].values()), ["A", "B"])
        out = sanitize_transcript("Option A beat variant: B and (A); \"B\" lost. A file was read by a tool.",
                                  extra_labels=["A", "B"])["sanitized_text"]
        for leaked in ("Option A", "variant: B", "(A)", '"B"'):
            self.assertNotIn(leaked, out)
        self.assertIn("A file was read by a tool.", out)
        self.assertEqual(out.count("[REDACTED_LABEL]"), 4)

    def test_metadata_identity_keys_are_stripped_and_redacted_without_cli_labels(self):
        from factonio.eval_skills import evaluate_blind_ab

        a = {"candidate": "skill-alpha", "variant_name": "terse-prompt", "run_name": "nightly-17",
             "worker_session_id": "local-deadbeef", "task_id": "t_0123abcd",
             "messages": [{"role": "assistant", "content": "skill-alpha (terse-prompt, nightly-17) ran python3 -m unittest"}]}
        b = {"skill_variant": "skill-beta", "name": "verbose-prompt",
             "messages": [{"role": "assistant", "content": "I am skill-beta / verbose-prompt; ran pytest"}]}
        res = evaluate_blind_ab(a, b, phase="implement", seed="m")
        self.assertEqual(sorted(res["key"].values()), ["skill-alpha", "skill-beta"])
        packet = json.dumps(res["packet"])
        for leaked in ("skill-alpha", "skill-beta", "terse-prompt", "verbose-prompt", "nightly-17",
                       "local-deadbeef", "t_0123abcd", "worker_session_id", "variant_name", "run_name"):
            self.assertNotIn(leaked, packet, leaked)
        self.assertIn("python3 -m unittest", packet)

    def test_tool_call_name_is_not_mistaken_for_identity(self):
        from factonio.eval_skills import evaluate_blind_ab

        leaky = {"type": "tool_use", "name": "Read", "input": {"file_path": "holdout/s1.md"}}
        res = evaluate_blind_ab({"label": "clean", "transcript": "$ python3 -m unittest"},
                                {"label": "leaky", "transcript": [leaky]}, phase="implement", seed=1)
        self.assertEqual([res["key"][s] for s in res["disqualified"]], ["leaky"])


class TestFixRound1CommandParsing(unittest.TestCase):
    """Fixes 2, 4, 5: command-aware flags, normalized containment, grep pattern vs file operands."""

    def test_display_flags_never_exclude_the_file_operand(self):
        from factonio.eval_skills import audit_barrier_leaks

        for cmd in ("cat -v holdout/scenario.md", "cat -n holdout/scenario.md", "head -n 20 holdout/s.md",
                    "tail -n 20 holdout/s.md", "nl -b a holdout/s.md", "grep -v TODO holdout/s.md",
                    "grep -f holdout/patterns.txt README.md"):
            with self.subTest(cmd=cmd):
                res = audit_barrier_leaks(_term(cmd), phase="implement")
                self.assertTrue(res["leaked"], res)
                self.assertEqual(res["findings"][0]["rule"], "holdout_access")
        for cmd in ("grep -v holdout README.md", "rg -e holdout README.md", "rg --regexp=holdout docs"):
            with self.subTest(cmd=cmd):
                self.assertTrue(audit_barrier_leaks(_term(cmd), phase="implement")["clean"], cmd)

    def test_allowlist_uses_normalized_lexical_containment(self):
        from factonio.eval_skills import audit_barrier_leaks

        for path in (".factonio/../src/app.py", "holdout/../../src/app.py", ".factonio\\..\\src\\app.py",
                     "design/../src/app.py", "src/.factonio/helper.py", "./src/../src/app.py"):
            with self.subTest(path=path):
                res = audit_barrier_leaks([{"tool": "read_file", "args": {"path": path}}], phase="verify-blackbox")
                self.assertEqual([f["rule"] for f in res["findings"]], ["source_read"], res)
                self.assertTrue(audit_barrier_leaks(f"$ cat '{path}'", phase="verify-blackbox")["leaked"])
        for path in (".factonio/drive.sh", "./holdout/helper.py", "design/preview.js",
                     "/runs/acme/holdout/helper.py", "/runs/acme/.factonio/drive.sh"):
            with self.subTest(path=path):
                res = audit_barrier_leaks([{"tool": "read_file", "args": {"path": path}}], phase="verify-blackbox")
                self.assertTrue(res["clean"], res)

    def test_grep_patterns_are_not_file_operands(self):
        from factonio.eval_skills import audit_barrier_leaks

        clean = ["rg 'app.py' README.md", "grep -n app.py README.md", "rg -e app.py -e main.go README.md",
                 "grep -m 3 -A 2 'auth.py' docs/usage.md", "rg -i --max-count 1 handler.ts README.md"]
        for cmd in clean:
            with self.subTest(cmd=cmd):
                res = audit_barrier_leaks(_term(cmd), phase="verify-blackbox")
                self.assertTrue(res["clean"], (cmd, res["findings"]))
        leaks = {"rg TODO src/app.py": "src/app.py", "grep -e foo -- lib/auth.py": "lib/auth.py",
                 "rg -n -m 5 TODO src/": "src/", "rg -g '*.py' def": "*.py", "grep -f lib/patterns.rb README.md": "lib/patterns.rb"}
        for cmd, path in leaks.items():
            with self.subTest(cmd=cmd):
                res = audit_barrier_leaks(_term(cmd), phase="verify-blackbox")
                self.assertEqual([(f["rule"], f["path"]) for f in res["findings"]], [("source_read", path)], cmd)


class TestFixRound1CodeReads(unittest.TestCase):
    """Fix 3: statically visible reads in execute_code, interpreter one-liners and tool wrappers."""

    def test_blackbox_code_and_interpreter_reads_are_flagged(self):
        from factonio.eval_skills import audit_barrier_leaks

        cases = {
            "code read_file": _code('from hermes_tools import read_file\nprint(read_file("src/auth.py"))'),
            "code hermes_tools.read_file": _code('import hermes_tools\nhermes_tools.read_file(path="src/auth.py")'),
            "code open": _code('print(open("src/auth.py").read())'),
            "code Path.read_text": _code('from pathlib import Path\nprint(Path("src/auth.py").read_text())'),
            "code search_files": _code('search_files("def login", path="src")'),
            "code subprocess list": _code('import subprocess\nsubprocess.run(["cat", "src/app.py"])'),
            "code terminal wrapper": _code('terminal("cat lib/app.rb")'),
            "python3 -c": _term("python3 -c \"print(open('src/auth.py').read())\""),
            "python -c pathlib": _term("python -c \"from pathlib import Path; print(Path('src/app.py').read_text())\""),
            "node -e": _term("node -e \"fs.readFileSync('src/app.js')\""),
            "nested sh -c": _term("sh -c \"python3 -c 'open(\\\"src/a.py\\\").read()'\""),
            "tool_call wrapper": [{"name": "tool_call", "arguments": {"calls": [
                {"name": "read_file", "arguments": {"path": "src/app.py"}}]}}],
            "parallel wrapper": [{"tool": "multi_tool_use.parallel", "args": {"tool_uses": [
                {"recipient_name": "functions.terminal", "parameters": {"command": "cat src/app.py"}}]}}],
        }
        for label, transcript in cases.items():
            with self.subTest(case=label):
                res = audit_barrier_leaks(transcript, phase="verify-blackbox")
                self.assertEqual({f["rule"] for f in res["findings"]}, {"source_read"}, (label, res))
        diff = audit_barrier_leaks(_code('terminal("git diff HEAD~1")'), phase="verify-blackbox")
        self.assertEqual([f["rule"] for f in diff["findings"]], ["diff_inspection"])
        for ok in (_code('print(open("README.md").read())'), _code('search_files("*.py", target="files", path="src")'),
                   _code('print(read_file("holdout/s1.md"))'), _term("python3 tools/eval_skills.py bloat skills")):
            with self.subTest(ok=ok):
                self.assertTrue(audit_barrier_leaks(ok, phase="verify-blackbox")["clean"])

    def test_holdout_reads_in_code_are_flagged_for_non_blackbox_phases(self):
        from factonio.eval_skills import audit_barrier_leaks

        for transcript in (_code('print(read_file("holdout/s1.md"))'), _term("python3 -c \"open('holdout/s1.md')\""),
                           _code('import subprocess; subprocess.run(["cat", "holdout/s1.md"])')):
            with self.subTest(transcript=transcript):
                res = audit_barrier_leaks(transcript, phase="implement")
                self.assertEqual([f["rule"] for f in res["findings"]], ["holdout_access"], res)

    def test_code_source_reads_disqualify_the_candidate(self):
        from factonio.eval_skills import evaluate_blind_ab

        for call in (_code('from hermes_tools import read_file; print(read_file("src/app.py"))'),
                     _term("python3 -c 'from pathlib import Path; print(Path(\"src/app.py\").read_text())'")):
            with self.subTest(call=call):
                res = evaluate_blind_ab({"label": "source-reader", "transcript": call},
                                        {"label": "clean", "transcript": []}, phase="verify-blackbox")
                self.assertEqual([res["key"][s] for s in res["disqualified"]], ["source-reader"])
                self.assertEqual(res["winner_label"], "clean")


class TestFixRound1RoleInheritance(unittest.TestCase):
    """Fix 6: quoted JSON inside user/system/tool messages is never an executed call."""

    def test_quoted_json_in_non_action_roles_is_ignored(self):
        from factonio.eval_skills import audit_barrier_leaks

        call = {"tool": "read_file", "args": {"path": "holdout/scenario.md"}}
        nested = [{"role": "assistant", "tool_calls": [
            {"function": {"name": "read_file", "arguments": json.dumps({"path": "holdout/a.md"})}}]}]
        for role in ("user", "system", "tool"):
            for content in (json.dumps(call), json.dumps(nested), json.dumps(call) + "\n" + json.dumps(call)):
                with self.subTest(role=role, content=content[:30]):
                    res = audit_barrier_leaks({"role": role, "content": content}, phase="implement")
                    self.assertTrue(res["clean"], res)

    def test_top_level_json_and_jsonl_transcripts_still_decode(self):
        from factonio.eval_skills import audit_barrier_leaks

        call = {"tool": "read_file", "args": {"path": "holdout/scenario.md"}}
        self.assertTrue(audit_barrier_leaks(json.dumps([call]), phase="implement")["leaked"])
        jsonl = "\n".join(json.dumps(x) for x in ({"role": "user", "content": "go"}, call))
        self.assertTrue(audit_barrier_leaks(jsonl, phase="implement")["leaked"])
        quoted_then_real = [{"role": "user", "content": json.dumps(call)},
                            {"role": "assistant", "content": json.dumps(call)}]
        self.assertEqual(len(audit_barrier_leaks(quoted_then_real, phase="implement")["findings"]), 1)


class TestFixRound1StrictVendor(unittest.TestCase):
    """Fix 7: strict cross-vendor independence fails closed before judge_fn is invoked."""

    def test_provider_only_candidates_resolve_and_conflict(self):
        from factonio.eval_skills import evaluate_blind_ab

        called: list[int] = []
        with self.assertRaisesRegex(ValueError, "strict_vendor"):
            evaluate_blind_ab({"label": "a", "provider": "openai", "text": "a"},
                              {"label": "b", "model": "claude-sonnet-4", "text": "b"}, phase="implement",
                              strict_vendor=True, judge_model="gpt-5", judge_fn=lambda p: called.append(1) or "X")
        self.assertEqual(called, [])
        loose = evaluate_blind_ab({"label": "a", "provider": "openai", "text": "a"},
                                  {"label": "b", "model": "claude-sonnet-4", "text": "b"}, phase="implement",
                                  judge_model="gpt-5")
        self.assertEqual(loose["judge"]["candidate_vendors"], ["anthropic", "openai"])
        self.assertFalse(loose["judge"]["independent"])

    def test_unknown_candidate_or_judge_fails_closed_before_judging(self):
        from factonio.eval_skills import evaluate_blind_ab

        known = {"label": "b", "model": "claude-sonnet-4", "text": "b"}
        cases = [
            ({"label": "a", "model": "mystery-model-1", "text": "a"}, {"judge_model": "gemini-2.5-pro"}),
            ({"label": "a", "text": "a"}, {"judge_model": "gemini-2.5-pro"}),
            ({"label": "a", "model": "gpt-5", "text": "a"}, {}),
            ({"label": "a", "model": "gpt-5", "text": "a"}, {"judge_model": "mystery-judge"}),
            ({"label": "a", "model": "gpt-5", "text": "a"}, {"judge_provider": "openrouter"}),
            ({"label": "a", "text": "a"}, {"judge_fn": None}),
        ]
        for cand, extra in cases:
            with self.subTest(cand=cand, extra=sorted(extra)):
                called: list[int] = []
                kwargs = dict(extra)
                if "judge_fn" not in kwargs or kwargs["judge_fn"] is not None:
                    kwargs["judge_fn"] = lambda p: called.append(1) or "X"
                with self.assertRaisesRegex(ValueError, "strict_vendor"):
                    evaluate_blind_ab(cand, known, phase="implement", strict_vendor=True, **kwargs)
                self.assertEqual(called, [])
        loose = evaluate_blind_ab({"label": "a", "model": "mystery-model-1", "text": "a"}, known,
                                  phase="implement", judge_model="gemini-2.5-pro")
        self.assertFalse(loose["judge"]["independent"])
        self.assertEqual(loose["judge"]["unresolved_candidate_vendors"], 1)
        self.assertTrue(any("unknown" in a for a in loose["advisories"]))

    def test_strict_passes_with_known_independent_judge_or_offline_known_candidates(self):
        from factonio.eval_skills import evaluate_blind_ab

        a = {"label": "a", "provider": "openai", "text": "a"}
        b = {"label": "b", "model": "claude-sonnet-4", "text": "b"}
        judged = evaluate_blind_ab(a, b, phase="implement", strict_vendor=True, judge_provider="google",
                                   judge_fn=lambda p: "X")
        self.assertEqual((judged["judge"]["vendor"], judged["judge"]["independent"]), ("google", True))
        offline = evaluate_blind_ab(a, b, phase="implement", strict_vendor=True)
        self.assertIsNone(offline["judge"]["independent"])
        self.assertEqual(offline["mode"], "offline")


class TestFixRound1StructuredCompletion(unittest.TestCase):
    """Fix 8: top-level completion fields beside `messages` reach the packet and the rubric."""

    def test_completion_fields_are_preserved_scored_and_identity_stripped(self):
        from factonio.eval_skills import evaluate_blind_ab

        structured = {
            "model": "gpt-5", "provider": "openai", "vendor": "openai", "candidate": "bb-v2", "label": "bb-v2",
            "task_id": "t_71324f91", "worker_session_id": "local-abc123",
            "messages": [{"role": "assistant", "content": "Ran in an isolated HERMES_HOME via mktemp."}],
            "verdict": "pass", "scenarios_run": 5, "scenarios_passed": 5,
            "findings": [{"scenario": "S1", "evidence": "exit code 0 per .factonio/verify.md"}],
            "head_sha": SHA, "metadata": {"model": "gpt-5", "checks_run": ["unittest"]},
        }
        bare = {"label": "bb-v1", "messages": [{"role": "assistant", "content": "Ran in mktemp."}]}
        res = evaluate_blind_ab(structured, bare, phase="verify-blackbox", seed="s")
        slot = next(s for s in ("X", "Y") if res["key"][s] == "bb-v2")
        rubric = res["candidates"][slot]["rubric"]
        self.assertEqual(rubric["score"], 1.0, rubric["failed"])
        self.assertEqual(res["winner_label"], "bb-v2")
        text = res["packet"]["candidates"][slot]
        for kept in ('"verdict": "pass"', '"scenarios_run": 5', '"findings"', "checks_run"):
            self.assertIn(kept, text)
        for leaked in ("gpt-5", "openai", "bb-v2", "t_71324f91", "local-abc123", '"model"', '"provider"',
                       '"vendor"', '"candidate"', '"label"', '"task_id"', '"worker_session_id"'):
            self.assertNotIn(leaked, text, leaked)


class TestFixRound1Cli(unittest.TestCase):
    """Fixes 9 and 10 on `hermes factonio eval` and the standalone `python3 -S` entrypoint."""

    SCRIPT = PLUGIN_ROOT / "tools" / "eval_skills.py"

    def _standalone(self, *args: str, home: Path) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env.update({"HOME": str(home.parent), "HERMES_HOME": str(home)})
        return subprocess.run([sys.executable, "-S", str(self.SCRIPT), *args], capture_output=True, text=True,
                              cwd=str(PLUGIN_ROOT), env=env, timeout=60)

    def _files(self, root: Path) -> tuple[Path, Path]:
        a, b = root / "a.json", root / "b.json"
        a.write_text(json.dumps({"label": "skill-v1", "model": "claude-opus-4", "transcript": GOOD_IMPLEMENT}))
        b.write_text(json.dumps({"label": "skill-v2", "model": "gpt-5", "transcript": LEAKY_IMPLEMENT}))
        return a, b

    def test_ab_dry_run_never_writes_trail(self):
        from factonio import trail

        with isolated_hermes_home() as home:
            run = TestEvalCli()._cli()
            a, b = self._files(home)
            argv = ("eval", "ab", str(a), str(b), "--phase", "implement", "--seed", "1", "--json")
            code, out = run(*argv, "--dry-run", "--record-trail")
            self.assertEqual(code, 0, out)
            res = json.loads(out)
            self.assertEqual((res["dry_run"], res["trail_decision_id"], res["winner_label"]), (True, None, "skill-v1"))
            self.assertEqual(trail.query_decisions(phase="eval"), [])
            code2, out2 = run(*argv, "--dry-run")
            self.assertEqual(json.loads(out2), res)
            self.assertFalse((home / "plugin-data" / "factonio" / "trail.db").exists())
            code3, out3 = run(*argv, "--record-trail")
            self.assertEqual(code3, 0)
            self.assertFalse(json.loads(out3)["dry_run"])
            self.assertEqual(len(trail.query_decisions(phase="eval")), 1)

            st = self._standalone("ab", str(a), str(b), "--phase", "implement", "--seed", "1", "--dry-run",
                                  "--record", "--json", home=home)
            self.assertEqual(st.returncode, 0, st.stderr)
            sres = json.loads(st.stdout)
            self.assertEqual((sres["dry_run"], sres["trail_decision_id"]), (True, None))
            self.assertEqual(sres["key"], res["key"])
            self.assertEqual(len(trail.query_decisions(phase="eval")), 1)

    def test_bloat_accepts_skill_directories(self):
        expected = sorted(str(p) for p in (PLUGIN_ROOT / "skills").rglob("SKILL.md"))
        self.assertEqual(len(expected), 6)
        with isolated_hermes_home() as home:
            run = TestEvalCli()._cli()
            code, out = run("eval", "bloat", str(PLUGIN_ROOT / "skills"), "--json")
            res = json.loads(out)
            self.assertTrue(res["ok"], out)
            self.assertEqual([r["source"] for r in res["results"]], expected)
            # Measurement succeeded: exit 0 by default even when a skill is over_budget.
            self.assertEqual(code, 0)
            code, out = run("eval", "bloat", str(PLUGIN_ROOT / "skills"), "--hard-tokens", "100000", "--json")
            self.assertEqual((code, json.loads(out)["over_budget"]), (0, False))
            code, out = run("eval", "bloat", str(PLUGIN_ROOT / "skills" / "implement"), "--json")
            self.assertEqual([r["source"] for r in json.loads(out)["results"]],
                             [str(PLUGIN_ROOT / "skills" / "implement" / "SKILL.md")])
            notes = home / "notes"
            (notes / "sub").mkdir(parents=True)
            (notes / "b.md").write_text("# B\n")
            (notes / "sub" / "a.md").write_text("# A\n")
            code, out = run("eval", "bloat", str(notes), "--json")
            self.assertEqual([Path(r["source"]).name for r in json.loads(out)["results"]], ["b.md", "a.md"])
            (home / "empty").mkdir()
            code, out = run("eval", "bloat", str(home / "empty"), "--json")
            self.assertEqual(code, 1)
            self.assertIn("no SKILL.md", json.loads(out)["error"])

            st = self._standalone("bloat", "skills", "--json", home=home)
            sres = json.loads(st.stdout)
            self.assertTrue(sres["ok"], st.stdout + st.stderr)
            self.assertEqual(len(sres["results"]), 6)
            self.assertEqual(st.returncode, 0, st.stderr)

    def test_standalone_strict_vendor_and_metadata_labels(self):
        with isolated_hermes_home() as home:
            a, b = home / "a.json", home / "b.json"
            a.write_text(json.dumps({"candidate": "Z", "messages": [
                {"role": "assistant", "content": "Candidate Z ran python3 -m unittest"}]}))
            b.write_text(json.dumps({"variant": "W", "messages": [
                {"role": "assistant", "content": "Variant W ran pytest"}]}))
            st = self._standalone("ab", str(a), str(b), "--phase", "implement", "--dry-run", "--json", home=home)
            self.assertEqual(st.returncode, 0, st.stderr)
            res = json.loads(st.stdout)
            self.assertEqual(sorted(res["key"].values()), ["W", "Z"])
            self.assertNotIn("Candidate Z", json.dumps(res["packet"]))
            self.assertNotIn("Variant W", json.dumps(res["packet"]))
            strict = self._standalone("ab", str(a), str(b), "--phase", "implement", "--strict-vendor", "--json",
                                      home=home)
            self.assertEqual(strict.returncode, 1)
            self.assertIn("strict_vendor", json.loads(strict.stdout)["error"])
            ok = self._standalone("ab", str(a), str(b), "--phase", "implement", "--strict-vendor", "--provider-a",
                                  "openai", "--model-b", "claude-sonnet-4", "--judge-provider", "google", "--json",
                                  home=home)
            self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
            self.assertEqual(json.loads(ok.stdout)["judge"]["candidate_vendors"], ["anthropic", "openai"])
            leak = home / "leak.json"
            leak.write_text(json.dumps([{"tool": "terminal", "args": {"command": "python3 -c \"print(open('src/auth.py').read())\""}}]))
            audit = self._standalone("audit", str(leak), "--phase", "verify-blackbox", "--json", home=home)
            self.assertEqual(audit.returncode, 1)
            self.assertEqual(json.loads(audit.stdout)["results"][0]["findings"][0]["rule"], "source_read")


class TestFixRound2EnvelopeRoles(unittest.TestCase):
    """Fix round 2, finding 1: enclosing non-action roles survive container traversal."""

    def test_non_action_envelopes_suppress_nested_assistant_calls_in_every_container(self):
        from factonio.eval_skills import audit_barrier_leaks

        inner = [{"role": "assistant", "tool": "read_file", "args": {"path": "holdout/case.md"}}]
        for role in ("user", "system", "tool"):
            for key in ("messages", "events", "transcript"):
                for value in (inner, json.dumps(inner), "\n".join(json.dumps(x) for x in inner * 2)):
                    with self.subTest(role=role, key=key, value=str(value)[:20]):
                        envelope = {"role": role, key: value}
                        self.assertTrue(audit_barrier_leaks(envelope, phase="implement")["clean"])
                        self.assertTrue(audit_barrier_leaks([envelope], phase="implement")["clean"])
                        quoted = {"role": role, "content": json.dumps(envelope)}
                        self.assertTrue(audit_barrier_leaks(quoted, phase="implement")["clean"])

    def test_assistant_and_unattributed_containers_still_audit_nested_calls(self):
        from factonio.eval_skills import audit_barrier_leaks

        inner = [{"role": "assistant", "tool": "read_file", "args": {"path": "holdout/case.md"}}]
        for envelope in ({"messages": inner}, {"role": "assistant", "events": inner}, {"transcript": json.dumps(inner)}):
            with self.subTest(envelope=str(envelope)[:40]):
                res = audit_barrier_leaks(envelope, phase="implement")
                self.assertEqual([f["rule"] for f in res["findings"]], ["holdout_access"], res)


class TestFixRound2CodeOperations(unittest.TestCase):
    """Fix round 2, finding 2: only executable operations in code count, never printed prose."""

    def test_printed_prohibitions_and_comments_are_clean(self):
        from factonio.eval_skills import audit_barrier_leaks

        for phase, code in (("implement", 'print("Never read holdout/case.md")'),
                            ("implement", '# do not open holdout/\nx = "holdout/case.md is off limits"\nprint(x)'),
                            ("verify-blackbox", 'print("Never run git diff")'),
                            ("verify-blackbox", '"""Never git show HEAD."""\nprint("ok")')):
            with self.subTest(phase=phase, code=code):
                res = audit_barrier_leaks(_code(code), phase=phase)
                self.assertTrue(res["clean"], res)

    def test_executable_operations_in_code_are_flagged(self):
        from factonio.eval_skills import audit_barrier_leaks

        holdout = ('open("holdout/case.md").read()', 'import os\nos.listdir("holdout")',
                   'import glob\nglob.glob("holdout/*.md")',
                   'open(os.path.join("holdout", "case.md"))', 'os.system("cat holdout/case.md")')
        for code in holdout:
            with self.subTest(code=code):
                res = audit_barrier_leaks(_code(code), phase="implement")
                self.assertEqual([f["rule"] for f in res["findings"]], ["holdout_access"], res)
        for code in ('import subprocess\nsubprocess.run("git diff", shell=True)',
                     'import subprocess\nsubprocess.run(["git", "diff"])', 'import os\nos.system("git show HEAD")',
                     'import hermes_tools\nhermes_tools.terminal("gh pr diff 31")'):
            with self.subTest(code=code):
                res = audit_barrier_leaks(_code(code), phase="verify-blackbox")
                self.assertEqual([f["rule"] for f in res["findings"]], ["diff_inspection"], res)


class TestFixRound2DenoReads(unittest.TestCase):
    """Fix round 2, finding 3: Deno and Bun file-read APIs are code reads."""

    def test_deno_and_bun_reads_are_flagged(self):
        from factonio.eval_skills import audit_barrier_leaks

        for api in ("Deno.readTextFileSync", "Deno.readTextFile", "Deno.readFileSync", "Deno.readFile",
                    "Deno.open", "Deno.openSync", "Bun.file"):
            cmd = f"deno eval \"console.log({api}('src/auth.py'))\""
            with self.subTest(api=api):
                res = audit_barrier_leaks(_term(cmd), phase="verify-blackbox")
                self.assertEqual([(f["rule"], f["path"]) for f in res["findings"]], [("source_read", "src/auth.py")])
                held = audit_barrier_leaks(_term(cmd.replace("src/auth.py", "holdout/case.md")), phase="implement")
                self.assertEqual([f["rule"] for f in held["findings"]], ["holdout_access"])
        self.assertTrue(audit_barrier_leaks(_term("deno eval \"console.log(Deno.readTextFileSync('README.md'))\""),
                                            phase="verify-blackbox")["clean"])

    def test_deno_read_disqualifies_candidate(self):
        from factonio.eval_skills import evaluate_blind_ab

        reader = _term("deno eval \"console.log(Deno.readTextFileSync('src/auth.py'))\"")
        res = evaluate_blind_ab({"label": "deno", "transcript": reader}, {"label": "clean", "transcript": []},
                                phase="verify-blackbox")
        self.assertEqual([res["key"][s] for s in res["disqualified"]], ["deno"])


class TestFixRound2RecursiveSearch(unittest.TestCase):
    """Fix round 2, finding 4: recursive searches over the repo root or source dirs are source reads."""

    def test_default_and_root_recursive_searches_are_flagged(self):
        from factonio.eval_skills import audit_barrier_leaks

        for cmd in ("rg secret", "grep -R password .", "grep -rn SECRET .", "grep --recursive token ./",
                    "egrep -r key", "ag secret", "ack secret", "git grep secret", "rg secret . README.md",
                    "grep -r secret tests/", "rg -i secret src"):
            with self.subTest(cmd=cmd):
                res = audit_barrier_leaks(_term(cmd), phase="verify-blackbox")
                self.assertTrue(res["leaked"], res)
                self.assertEqual({f["rule"] for f in res["findings"]}, {"source_read"})

    def test_searches_restricted_to_permitted_roots_stay_clean(self):
        from factonio.eval_skills import audit_barrier_leaks

        for cmd in ("rg 'app.py' README.md", "rg foo .factonio/", "rg foo .factonio", "grep -r foo holdout/",
                    "rg foo docs/", "rg foo spec.md design/", "grep password README.md", "grep -r foo .factonio/ docs",
                    "rg -g '*.md' foo", "rg --files"):
            with self.subTest(cmd=cmd):
                res = audit_barrier_leaks(_term(cmd), phase="verify-blackbox")
                self.assertTrue(res["clean"], (cmd, res["findings"]))


class TestFixRound2InputRedirection(unittest.TestCase):
    """Fix round 2, finding 5: `<` input-redirection operands are file reads regardless of the verb."""

    def test_redirected_source_reads_are_flagged(self):
        from factonio.eval_skills import audit_barrier_leaks

        cases = {"sort < src/app.py": "src/app.py", "cat < src/auth.py": "src/auth.py",
                 "python3 - < src/auth.py": "src/auth.py", "wc -l 0< src/app.py": "src/app.py",
                 "cat <src/app.py": "src/app.py", "tr a b 0<src/app.py": "src/app.py",
                 "< src/app.py sort": "src/app.py", "sh -c 'sort < lib/x.rb'": "lib/x.rb"}
        for cmd, path in cases.items():
            with self.subTest(cmd=cmd):
                res = audit_barrier_leaks(_term(cmd), phase="verify-blackbox")
                self.assertEqual([(f["rule"], f["path"]) for f in res["findings"]], [("source_read", path)], res)
        res = audit_barrier_leaks(_code('import subprocess\nsubprocess.run("sort < src/app.py", shell=True)'),
                                  phase="verify-blackbox")
        self.assertEqual([f["rule"] for f in res["findings"]], ["source_read"])
        for cmd in ("sort < holdout/case.md", "python3 - 0< holdout/case.md"):
            with self.subTest(cmd=cmd):
                res = audit_barrier_leaks(_term(cmd), phase="implement")
                self.assertEqual([f["rule"] for f in res["findings"]], ["holdout_access"], res)

    def test_permitted_inputs_output_redirections_and_heredocs_stay_clean(self):
        from factonio.eval_skills import audit_barrier_leaks

        for cmd in ("cat < .factonio/verify.md", "echo hi > out.py", "echo hi >> src/app.py",
                    "python3 -m unittest > out.log 2>&1", "make test 2> err.py", "cat <<EOF\nhello\nEOF",
                    "cat <<< src/app.py", "sort 0<&3", "cmd >&2"):
            with self.subTest(cmd=cmd):
                res = audit_barrier_leaks(_term(cmd), phase="verify-blackbox")
                self.assertTrue(res["clean"], (cmd, res["findings"]))


def _search(**args: Any) -> list[dict[str, Any]]:
    return [{"role": "assistant", "tool_calls": [{"function": {"name": "search_files", "arguments": json.dumps(args)}}]}]


class TestFixRound3StructuredContentSearch(unittest.TestCase):
    """Fix round 3, finding 1: `search_files` content searches use recursive-search root classification."""

    LEAKS = {
        "omitted path": {"pattern": "SECRET", "target": "content"},
        "omitted target and path": {"pattern": "SECRET"},
        "dot": {"pattern": "SECRET", "target": "content", "path": "."},
        "dot slash": {"pattern": "SECRET", "target": "content", "path": "./"},
        "absolute home src": {"pattern": "SECRET", "target": "content", "path": "/home/alice/dev/repo/src"},
        "absolute checkout src": {"pattern": "SECRET", "target": "content", "path": "/checkout/src"},
        "absolute repo root": {"pattern": "SECRET", "target": "content", "path": "/checkout"},
        "relative tests dir": {"pattern": "SECRET", "target": "content", "path": "tests"},
        "source glob": {"pattern": "SECRET", "target": "content", "file_glob": "*.py"},
        "catch-all glob": {"pattern": "SECRET", "target": "content", "path": ".", "file_glob": "**/*"},
    }
    CLEAN = {
        "filename search": {"pattern": "*.py", "target": "files"},
        "filename search in src": {"pattern": "*.py", "target": "files", "path": "src"},
        ".factonio": {"pattern": "SECRET", "target": "content", "path": ".factonio"},
        ".factonio/": {"pattern": "SECRET", "target": "content", "path": ".factonio/"},
        "holdout": {"pattern": "SECRET", "target": "content", "path": "holdout"},
        "docs": {"pattern": "SECRET", "target": "content", "path": "docs"},
        "README.md": {"pattern": "SECRET", "target": "content", "path": "README.md"},
        "absolute run holdout": {"pattern": "SECRET", "target": "content", "path": "/runs/acme/holdout"},
        "absolute .factonio": {"pattern": "SECRET", "target": "content", "path": "/checkout/.factonio"},
        "glob into .factonio": {"pattern": "SECRET", "target": "content", "file_glob": ".factonio/**"},
        "markdown glob": {"pattern": "SECRET", "target": "content", "path": ".", "file_glob": "*.md"},
    }

    def test_content_searches_over_source_roots_are_source_reads(self):
        from factonio.eval_skills import audit_barrier_leaks

        for label, args in self.LEAKS.items():
            with self.subTest(case=label):
                res = audit_barrier_leaks(_search(**args), phase="verify-blackbox")
                self.assertTrue(res["leaked"], (label, res))
                self.assertEqual({f["rule"] for f in res["findings"]}, {"source_read"})
        for label, args in self.CLEAN.items():
            with self.subTest(clean=label):
                res = audit_barrier_leaks(_search(**args), phase="verify-blackbox")
                self.assertTrue(res["clean"], (label, res["findings"]))
        web = [{"role": "assistant", "tool_calls": [{"function": {"name": "web_search",
                                                                  "arguments": json.dumps({"query": "SECRET"})}}]}]
        self.assertTrue(audit_barrier_leaks(web, phase="verify-blackbox")["clean"])

    def test_code_tool_content_searches_are_source_reads(self):
        from factonio.eval_skills import audit_barrier_leaks

        for code in ('from hermes_tools import search_files\nsearch_files(pattern="SECRET", target="content", path=".")',
                     'import hermes_tools\nhermes_tools.search_files(pattern="SECRET", target="content")',
                     'search_files("SECRET")',
                     'search_files(pattern="SECRET", path="/home/alice/dev/repo/src")',
                     'search_files("SECRET", "content", "/checkout/src")'):
            with self.subTest(code=code):
                res = audit_barrier_leaks(_code(code), phase="verify-blackbox")
                self.assertEqual({f["rule"] for f in res["findings"]}, {"source_read"}, res)
        for code in ('search_files(pattern="*.py", target="files")', 'search_files("SECRET", path=".factonio")',
                     'search_files("SECRET", target="content", path="holdout/")'):
            with self.subTest(clean=code):
                self.assertTrue(audit_barrier_leaks(_code(code), phase="verify-blackbox")["clean"])

    def test_content_search_disqualifies_even_when_the_judge_picks_it(self):
        from factonio.eval_skills import evaluate_blind_ab

        for label in ("omitted path", "dot", "absolute home src"):
            with self.subTest(case=label):
                reader = {"label": "searcher", "transcript": _search(**self.LEAKS[label])}
                clean = {"label": "clean", "transcript": _search(**self.CLEAN[".factonio"])}
                res = evaluate_blind_ab(reader, clean, phase="verify-blackbox", seed="s",
                                        judge_fn=lambda packet: "X")
                self.assertEqual([res["key"][s] for s in res["disqualified"]], ["searcher"])
                self.assertEqual(res["winner_label"], "clean")


class TestFixRound3PythonAst(unittest.TestCase):
    """Fix round 3, finding 2: Python code is audited from real `ast.Call` nodes, not string text."""

    PRINTED = {
        "verify-blackbox": ("print(\"Never run subprocess.run('git diff')\")",
                            "print(\"Never call search_files(pattern='x', path='.')\")",
                            "import os\nprint('os.system(\"git show HEAD\") is forbidden')"),
        "implement": ("print(\"Never call read_file('holdout/case.md')\")",
                      "print('open(\"holdout/case.md\")')",
                      "def f():\n    \"\"\"Never open('holdout/case.md').\"\"\"\n    return 1"),
    }
    EXECUTED = {
        "verify-blackbox": (("import subprocess\nsubprocess.run(\"git diff\", shell=True)", "diff_inspection"),
                            ("import subprocess as sp\nsp.run(['git', 'diff'])", "diff_inspection"),
                            ("from subprocess import check_output\ncheck_output(['git', 'show', 'HEAD'])",
                             "diff_inspection"),
                            ("p = 'src/auth.py'\nprint(open(p).read())", "source_read"),
                            ("from pathlib import Path\nroot = Path('src')\n(root / 'auth.py').read_text()",
                             "source_read"),
                            ("exec(\"open('src/auth.py').read()\")", "source_read")),
        "implement": (("print(read_file(\"holdout/case.md\"))", "holdout_access"),
                      ("print(open(\"holdout/case.md\").read())", "holdout_access"),
                      ("name = 'case.md'\nopen(f'holdout/{name}')", "holdout_access")),
    }

    def test_printed_call_examples_are_clean(self):
        from factonio.eval_skills import audit_barrier_leaks, evaluate_blind_ab

        for phase, codes in self.PRINTED.items():
            for code in codes:
                with self.subTest(phase=phase, code=code):
                    self.assertTrue(audit_barrier_leaks(_code(code), phase=phase)["clean"])
                    res = evaluate_blind_ab({"label": "printer", "transcript": _code(code)},
                                            {"label": "other", "transcript": []}, phase=phase, seed="s")
                    self.assertEqual(res["disqualified"], [])
        one_liner = _term("python3 -c 'print(\"open(\\\"holdout/case.md\\\")\")'")
        self.assertTrue(audit_barrier_leaks(one_liner, phase="implement")["clean"])
        self.assertTrue(audit_barrier_leaks(_term("python3 -c 'print(\"git diff\")'"), phase="verify-blackbox")["clean"])

    def test_executed_calls_are_flagged_and_disqualified(self):
        from factonio.eval_skills import audit_barrier_leaks, evaluate_blind_ab

        for phase, cases in self.EXECUTED.items():
            for code, rule in cases:
                with self.subTest(phase=phase, code=code):
                    res = audit_barrier_leaks(_code(code), phase=phase)
                    self.assertEqual({f["rule"] for f in res["findings"]}, {rule}, res)
                    ab = evaluate_blind_ab({"label": "runner", "transcript": _code(code)},
                                           {"label": "other", "transcript": []}, phase=phase, seed="s",
                                           judge_fn=lambda packet: "X")
                    self.assertEqual([ab["key"][s] for s in ab["disqualified"]], ["runner"])
        held = audit_barrier_leaks(_term("python3 -c 'open(\"holdout/case.md\").read()'"), phase="implement")
        self.assertEqual([f["rule"] for f in held["findings"]], ["holdout_access"])

    def test_non_python_and_unparseable_code_fall_back_to_stripped_scanning(self):
        from factonio.eval_skills import audit_barrier_leaks

        for cmd in ("node -e \"console.log('never fs.readFileSync(\\'src/auth.py\\')')\"",
                    "node -e \"// fs.readFileSync('src/auth.py')\nconsole.log(1)\""):
            with self.subTest(clean=cmd):
                self.assertTrue(audit_barrier_leaks(_term(cmd), phase="verify-blackbox")["clean"])
        for cmd in ("node -e \"console.log(fs.readFileSync('src/auth.py', 'utf8'))\"",
                    "bun -e \"await Bun.file('src/auth.py').text()\"",
                    "deno eval \"Deno.readTextFileSync('src/auth.py')\""):
            with self.subTest(leak=cmd):
                res = audit_barrier_leaks(_term(cmd), phase="verify-blackbox")
                self.assertEqual({f["rule"] for f in res["findings"]}, {"source_read"}, res)
        broken = _code('print("never open(\'holdout/a.md\')"\nopen("holdout/case.md")  # syntax error above')
        self.assertEqual([f["rule"] for f in audit_barrier_leaks(broken, phase="implement")["findings"]],
                         ["holdout_access"])


class TestFixRound3Heredocs(unittest.TestCase):
    """Fix round 3, finding 3: heredoc bodies are data unless an interpreter reads them as its program."""

    def test_passive_heredoc_bodies_are_clean(self):
        from factonio.eval_skills import audit_barrier_leaks, evaluate_blind_ab

        for phase, cmd in (("verify-blackbox", "cat <<'EOF'\nNever read src/private.py\nEOF"),
                           ("verify-blackbox", "cat <<EOF > notes.md\ncat src/private.py is forbidden\nEOF"),
                           ("verify-blackbox", "tee report.md <<-\"END\"\n\tgit diff is off limits\n\tEND\necho done"),
                           ("implement", "cat <<'EOF'\nNever open holdout/case.md\nEOF"),
                           ("implement", "gh pr comment 31 --body-file - <<'EOF'\nNo holdout/ reads.\nEOF")):
            with self.subTest(phase=phase, cmd=cmd):
                self.assertTrue(audit_barrier_leaks(_term(cmd), phase=phase)["clean"])
        res = evaluate_blind_ab({"label": "heredoc", "transcript": _term("cat <<'EOF'\nNever read src/private.py\nEOF")},
                                {"label": "other", "transcript": []}, phase="verify-blackbox", seed="s")
        self.assertEqual(res["disqualified"], [])

    def test_interpreter_heredocs_are_audited_as_programs(self):
        from factonio.eval_skills import audit_barrier_leaks, evaluate_blind_ab

        cases = (("verify-blackbox", "python3 - <<'EOF'\nopen('src/private.py').read()\nEOF", "source_read"),
                 ("verify-blackbox", "python3 <<EOF\nprint(open('src/private.py').read())\nEOF", "source_read"),
                 ("verify-blackbox", "bash <<'EOF'\ncd /tmp\ncat src/private.py\nEOF", "source_read"),
                 ("verify-blackbox", "node <<'EOF'\nrequire('fs').readFileSync('src/a.js')\nEOF", "source_read"),
                 ("verify-blackbox", "cat <<'EOF' | python3 -\nimport subprocess\nsubprocess.run(['git', 'diff'])\nEOF",
                  "diff_inspection"),
                 ("verify-blackbox", "echo 'git show HEAD' | sh", "diff_inspection"),
                 ("verify-blackbox", "python3 <<< \"open('src/private.py')\"", "source_read"),
                 ("implement", "python3 - <<'EOF'\nopen('holdout/case.md').read()\nEOF", "holdout_access"),
                 ("implement", "sh <<EOF\ncat holdout/case.md\nEOF", "holdout_access"))
        for phase, cmd, rule in cases:
            with self.subTest(cmd=cmd):
                res = audit_barrier_leaks(_term(cmd), phase=phase)
                self.assertEqual({f["rule"] for f in res["findings"]}, {rule}, res)
        reader = _term("python3 - <<'EOF'\nopen('src/private.py').read()\nEOF")
        ab = evaluate_blind_ab({"label": "reader", "transcript": reader}, {"label": "other", "transcript": []},
                               phase="verify-blackbox", seed="s", judge_fn=lambda packet: "X")
        self.assertEqual([ab["key"][s] for s in ab["disqualified"]], ["reader"])

    def test_multiline_commands_split_on_newlines(self):
        from factonio.eval_skills import audit_barrier_leaks

        res = audit_barrier_leaks(_term("cd repo\ncat src/private.py"), phase="verify-blackbox")
        self.assertEqual([f["path"] for f in res["findings"]], ["src/private.py"])
        quoted = _term("python3 -c '\nprint(\"cat src/private.py\")\n'")
        self.assertTrue(audit_barrier_leaks(quoted, phase="verify-blackbox")["clean"])


class TestFixRound3BloatExitCodes(unittest.TestCase):
    """Fix round 3, finding 4: `eval bloat` reports and exits 0; `--strict` gates on over_budget."""

    SCRIPT = PLUGIN_ROOT / "tools" / "eval_skills.py"

    def test_bundled_skills_measure_with_default_budgets(self):
        from factonio.eval_skills import DEFAULT_HARD_TOKENS, DEFAULT_SOFT_TOKENS

        self.assertEqual((DEFAULT_SOFT_TOKENS, DEFAULT_HARD_TOKENS), (3000, 4500))
        with isolated_hermes_home() as home:
            run = TestEvalCli()._cli()
            code, out = run("eval", "bloat", str(PLUGIN_ROOT / "skills"), "--json")
            res = json.loads(out)
            self.assertEqual(code, 0, out)
            self.assertEqual(len(res["results"]), 6)
            self.assertTrue(all((r["soft_tokens"], r["hard_tokens"]) == (3000, 4500) for r in res["results"]))
            for r in res["results"]:
                expected = "over_budget" if r["estimated_tokens"] > 4500 else "warn" if r["estimated_tokens"] > 3000 else "ok"
                self.assertEqual(r["status"], expected, r["source"])
            self.assertEqual(res["over_budget"], any(r["status"] == "over_budget" for r in res["results"]))
            self.assertFalse(res["fail_on_over_budget"])
            for flag in ("--strict", "--fail-on-over-budget"):
                code, out = run("eval", "bloat", str(PLUGIN_ROOT / "skills"), flag, "--json")
                self.assertEqual(code, 1 if json.loads(out)["over_budget"] else 0, flag)
                code, out = run("eval", "bloat", str(PLUGIN_ROOT / "skills"), flag, "--hard-tokens", "100000", "--json")
                self.assertEqual(code, 0, flag)
            code, out = run("eval", "bloat", str(PLUGIN_ROOT / "skills" / "implement"), "--hard-tokens", "10",
                            "--soft-tokens", "5", "--strict", "--json")
            self.assertEqual((code, json.loads(out)["results"][0]["status"]), (1, "over_budget"))
            code, out = run("eval", "bloat", str(home / "missing"), "--json")
            self.assertEqual(code, 1)
            code, out = run("eval", "audit", str(home / "missing"), "--phase", "implement", "--strict", "--json")
            self.assertEqual(code, 1)
            self.assertIn("only to bloat", json.loads(out)["error"])

            env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
            env.update({"HOME": str(home.parent), "HERMES_HOME": str(home)})
            for extra, expect in (((), 0), (("--hard-tokens", "10", "--soft-tokens", "5", "--strict"), 1)):
                st = subprocess.run([sys.executable, "-S", str(self.SCRIPT), "bloat", "skills", *extra, "--json"],
                                    capture_output=True, text=True, cwd=str(PLUGIN_ROOT), env=env, timeout=60)
                self.assertEqual(st.returncode, expect, st.stdout + st.stderr)
                self.assertEqual(len(json.loads(st.stdout)["results"]), 6)


class TestFixRound4DefaultLabelRedaction(unittest.TestCase):
    """Fix round 4, finding 1: default A/B identities never reach the judge packet."""

    SCRIPT = PLUGIN_ROOT / "tools" / "eval_skills.py"
    PROSE_A = "Candidate A: implemented the requested change. Found a bug and an issue; wrote a fix."
    PROSE_B = "Candidate B: implemented an alternative. Option A was slower than Model B and Run A."

    def _assert_blind(self, packet: dict[str, Any]) -> None:
        texts = [packet["candidates"][slot] for slot in ("X", "Y")]
        for text in texts:
            for leaked in ("Candidate A", "Candidate B", "Option A", "Model B", "Run A"):
                self.assertNotIn(leaked, text)
        joined = "\n".join(texts)
        for kept in ("Found a bug and an issue; wrote a fix.", "implemented an alternative"):
            self.assertIn(kept, joined)

    def test_direct_string_candidates_are_blinded(self):
        from factonio.eval_skills import evaluate_blind_ab

        res = evaluate_blind_ab(self.PROSE_A, self.PROSE_B, phase="implement", seed="7")
        self._assert_blind(res["packet"])
        self.assertEqual(sorted(res["key"].values()), ["A", "B"])

    def test_default_identity_forms_are_redacted_without_mangling_articles(self):
        from factonio.eval_skills import blind_packet, build_blind_pair

        a = "A: candidate_a ran tests.\nSkill A and Variant A agree. a fix for a bug; an issue remains."
        b = "B: candidate_b ran lint.\n**B:** Skill B and variant-b agree. Write a test."
        packet = blind_packet(build_blind_pair(a, b, seed="s"))
        joined = "\n".join(packet["candidates"].values())
        for leaked in ("A:", "B:", "candidate_a", "candidate_b", "Skill A", "Skill B", "Variant A", "variant-b"):
            self.assertNotIn(leaked, joined)
        self.assertIn("a fix for a bug; an issue remains.", joined)
        self.assertIn("Write a test.", joined)

    def test_standalone_file_cli_without_labels_is_blinded(self):
        with isolated_hermes_home() as home:
            (home / "A.json").write_text(json.dumps({"transcript": self.PROSE_A}))
            (home / "B.json").write_text(json.dumps({"transcript": self.PROSE_B}))
            env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
            env.update({"HOME": str(home.parent), "HERMES_HOME": str(home)})
            run = subprocess.run([sys.executable, "-S", str(self.SCRIPT), "ab", "A.json", "B.json", "--phase",
                                  "implement", "--seed", "7", "--json"], capture_output=True, text=True,
                                 cwd=str(home), env=env, timeout=60)
            self.assertEqual(run.returncode, 0, run.stderr)
            res = json.loads(run.stdout)
            self._assert_blind(res["packet"])
            self.assertEqual(sorted(res["key"].values()), ["A.json", "B.json"])


class TestFixRound4FlowSensitiveCode(unittest.TestCase):
    """Fix round 4, finding 2: variable values are resolved at the call's program point."""

    LEAKS = {
        "read then reassign": "p = 'src/app.py'\nprint(open(p).read())\np = 'README.md'",
        "semicolons": "p = 'src/auth.py'; open(p).read(); p = 'README.md'",
        "branch": "import sys\nif sys.argv:\n    p = 'src/app.py'\nelse:\n    p = 'README.md'\nopen(p)",
        "loop literal": "for p in ['README.md', 'src/app.py']:\n    open(p)",
        "loop back edge": "p = 'README.md'\nfor _ in range(2):\n    open(p).read()\n    p = 'src/app.py'",
        "function scope": "p = 'src/app.py'\ndef f():\n    p = 'README.md'\n    return p\nopen(p).read()",
        "closure late binding": "def f():\n    return open(p).read()\np = 'src/app.py'\nf()",
        "self reassign": "p = 'src/app.py'\np = open(p).read()",
        "augassign": "p = 'src/'\np += 'app.py'\nopen(p)",
        "try handler": "try:\n    p = 'src/app.py'\n    x = 1 / 0\nexcept Exception:\n    open(p)",
        "comprehension": "[open(p).read() for p in ('README.md', 'src/app.py')]",
        "path join": "import os\nd = 'src'\nopen(os.path.join(d, 'app.py'))\nd = 'docs'",
        "tuple swap": "a, b = 'src/app.py', 'README.md'\na, b = b, a\nopen(b)",
        "global write in function": "def f():\n    global p\n    p = 'src/app.py'\nf()\nopen(p)",
        "while back edge": "p = 'README.md'\nwhile True:\n    open(p)\n    p = 'src/app.py'",
    }
    CLEAN = {
        "reassigned before read": "p = 'src/app.py'\np = 'README.md'\nprint(open(p).read())",
        "printed prose": 'print("do not read src/app.py")',
        "function local only": "def f():\n    p = 'src/app.py'\n    p = 'README.md'\n    return open(p).read()\nf()",
        "unknown overwrite": "p = 'src/app.py'\np = input()\nopen(p)",
    }

    def test_execution_order_reads_are_flagged(self):
        from factonio.eval_skills import audit_barrier_leaks

        for label, code in self.LEAKS.items():
            with self.subTest(case=label):
                res = audit_barrier_leaks(_code(code), phase="verify-blackbox")
                self.assertEqual({f["rule"] for f in res["findings"]}, {"source_read"}, (label, res))
                self.assertFalse(res["clean"])

    def test_negative_controls_stay_clean(self):
        from factonio.eval_skills import audit_barrier_leaks

        for label, code in self.CLEAN.items():
            with self.subTest(case=label):
                res = audit_barrier_leaks(_code(code), phase="verify-blackbox")
                self.assertTrue(res["clean"], (label, res))

    def test_reassigned_reader_is_disqualified_in_ab(self):
        from factonio.eval_skills import evaluate_blind_ab

        for code in (self.LEAKS["read then reassign"], self.LEAKS["semicolons"]):
            with self.subTest(code=code):
                res = evaluate_blind_ab({"label": "leaker", "transcript": _code(code)},
                                        {"label": "clean", "transcript": []}, phase="verify-blackbox",
                                        seed="s", judge_fn=lambda packet: "X" if "src/" in packet["candidates"]["X"] else "Y")
                self.assertEqual([res["key"][s] for s in res["disqualified"]], ["leaker"])
                self.assertEqual(res["winner_label"], "clean")

    def test_audit_cli_exits_nonzero_for_reassigned_reader(self):
        with isolated_hermes_home() as home:
            path = home / "t.json"
            path.write_text(json.dumps(_code(self.LEAKS["semicolons"])))
            env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
            env.update({"HOME": str(home.parent), "HERMES_HOME": str(home)})
            run = subprocess.run([sys.executable, "-S", str(PLUGIN_ROOT / "tools" / "eval_skills.py"), "audit",
                                  str(path), "--phase", "verify-blackbox", "--json"], capture_output=True, text=True,
                                 cwd=str(home), env=env, timeout=60)
            self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
            res = json.loads(run.stdout)
            self.assertFalse(res["clean"])
            self.assertEqual([f["rule"] for f in res["results"][0]["findings"]], ["source_read"])


def _standalone_audit(code: str, home: Path, phase: str = "verify-blackbox") -> tuple[int, dict[str, Any]]:
    path = home / "t.json"
    path.write_text(json.dumps(_code(code)))
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update({"HOME": str(home.parent), "HERMES_HOME": str(home)})
    run = subprocess.run([sys.executable, "-S", str(PLUGIN_ROOT / "tools" / "eval_skills.py"), "audit", str(path),
                          "--phase", phase, "--json"], capture_output=True, text=True, cwd=str(home), env=env,
                         timeout=60)
    return run.returncode, json.loads(run.stdout)


def _forced_judge_ab(code: str, phase: str = "verify-blackbox") -> dict[str, Any]:
    """A/B where the judge always picks the candidate whose packet holds ``code``'s transcript."""
    from factonio.eval_skills import evaluate_blind_ab

    marker = "MARKER_SUBJECT"
    subject = {"label": "subject", "transcript": [{"role": "assistant", "content": marker}, *_code(code)]}
    other = {"label": "other", "transcript": [{"role": "assistant", "content": "ran the documented CLI only"}]}
    return evaluate_blind_ab(subject, other, phase=phase, seed="s",
                             judge_fn=lambda packet: "X" if marker in packet["candidates"]["X"] else "Y")


class TestFixRound5ArgvSequences(unittest.TestCase):
    """Fix round 5, finding 1: statically known argv lists/tuples survive variable binding."""

    LEAKS = {
        "assigned list": "import subprocess\ncmd = ['git', 'diff']\nsubprocess.run(cmd, capture_output=True)",
        "assigned tuple": "import subprocess\ncmd = ('git', 'diff')\nsubprocess.run(cmd)",
        "tuple show": "import subprocess\ncmd = ('git', 'show', 'HEAD')\nsubprocess.check_output(cmd)",
        "element variable": "import subprocess\np = 'src/app.py'\ncmd = ['cat', p]\nsubprocess.run(cmd)",
        "concatenation": "import subprocess\nbase = ['git']\ncmd = base + ['diff']\nsubprocess.Popen(cmd)",
        "augassign": "import subprocess\ncmd = ['git']\ncmd += ['diff']\nsubprocess.call(cmd)",
        "append": "import subprocess\ncmd = ['git']\ncmd.append('diff')\nsubprocess.check_call(cmd)",
        "extend": "import subprocess\ncmd = ['git']\ncmd.extend(['diff', '--stat'])\nsubprocess.run(cmd)",
        "insert": "import subprocess\ncmd = ['diff']\ncmd.insert(0, 'git')\nsubprocess.run(cmd)",
        "pop prefix": "import subprocess\ncmd = ['echo', 'git', 'diff']\ncmd.pop(0)\nsubprocess.run(cmd)",
        "alias": "import subprocess\ncmd = ['git', 'diff']\ncmd2 = cmd\nsubprocess.run(cmd2)",
        "alias mutated": "import subprocess\ncmd = ['git']\ncmd2 = cmd\ncmd.append('diff')\nsubprocess.run(cmd2)",
        "args keyword": "import subprocess\ncmd = ['git', 'diff']\nsubprocess.run(args=cmd)",
        "reassign after run": "import subprocess\ncmd = ['git', 'diff']\nsubprocess.run(cmd)\ncmd = ['echo', 'ok']",
        "branch": "import subprocess, sys\nif sys.argv:\n    cmd = ['git', 'diff']\nelse:\n    cmd = ['ls']\nsubprocess.run(cmd)",
        "list() copy": "import subprocess\ncmd = list(('git', 'diff'))\nsubprocess.run(cmd)",
        "shlex split": "import shlex, subprocess\ncmd = shlex.split('git diff')\nsubprocess.run(cmd)",
        "execvp argv": "import os\ncmd = ['git', 'diff']\nos.execvp('git', cmd)",
        "execv argv": "import os\nos.execv('/usr/bin/git', ['git', 'diff'])",
        "spawnlp varargs": "import os\nos.spawnlp(os.P_WAIT, 'git', 'git', 'diff')",
        "execl starred": "import os\ncmd = ['git', 'diff']\nos.execl('/usr/bin/git', *cmd)",
        "loop over commands": "import subprocess\nfor c in [['ls'], ['git', 'diff']]:\n    subprocess.run(c)",
        "loop over path list": "paths = ['README.md', 'src/app.py']\nfor p in paths:\n    open(p).read()",
        "loop over tuple var": "paths = ('README.md',)\npaths += ('src/app.py',)\nfor p in paths:\n    print(open(p).read())",
        "loop back edge": "import subprocess\ncmd = ['echo']\nfor _ in range(2):\n    subprocess.run(cmd)\n    cmd = ['git', 'diff']",
        "closure": "import subprocess\ndef f():\n    subprocess.run(cmd)\ncmd = ['git', 'diff']\nf()",
        "item assignment": "import subprocess\ncmd = ['echo', 'x']\ncmd[0] = 'git'\ncmd[1] = 'diff'\nsubprocess.run(cmd)",
        "del item": "import subprocess\ncmd = ['echo', 'git', 'diff']\ndel cmd[0]\nsubprocess.run(cmd)",
        "remove": "import subprocess\ncmd = ['echo', 'git', 'diff']\ncmd.remove('echo')\nsubprocess.run(cmd)",
        "slice": "import subprocess\ncmd = ['echo', 'git', 'diff']\nsubprocess.run(cmd[1:])",
        "unpack": "import subprocess\ncmd = ['git', 'diff']\na, b = cmd\nsubprocess.run([a, b])",
        "str split": "import subprocess\nsubprocess.run('git diff'.split())",
        "function mutates free list": "import subprocess\ncmd = ['git']\ndef f():\n    cmd.append('diff')\nf()\nsubprocess.run(cmd)",
    }
    CLEAN = {
        "reassign before run": "import subprocess\ncmd = ['git', 'diff']\ncmd = ['echo', 'ok']\nsubprocess.run(cmd)",
        "cleared then rebuilt": "import subprocess\ncmd = ['git', 'diff']\ncmd.clear()\ncmd.append('ls')\nsubprocess.run(cmd)",
        "popped suffix": "import subprocess\ncmd = ['git', 'status', 'diff']\ncmd.pop()\nsubprocess.run(cmd)",
        "printed argv": "cmd = ['git', 'diff']\nprint(cmd)",
        "function local only": ("import subprocess\ncmd = ['echo', 'ok']\ndef f():\n    cmd = ['git', 'diff']\n"
                                "    return cmd\nsubprocess.run(cmd)"),
        "unknown overwrite": "import subprocess\ncmd = ['git', 'diff']\ncmd = input().split(' ')\nsubprocess.run(cmd)",
        "loop over clean paths": "paths = ['README.md', 'spec.md']\nfor p in paths:\n    open(p).read()",
        "documented cli": "import subprocess\ncmd = ['python3', '-m', 'factonio', '--help']\nsubprocess.run(cmd)",
    }

    def test_assigned_argv_is_flagged(self):
        from factonio.eval_skills import audit_barrier_leaks

        for label, code in self.LEAKS.items():
            with self.subTest(case=label):
                res = audit_barrier_leaks(_code(code), phase="verify-blackbox")
                self.assertFalse(res["clean"], (label, res))
                self.assertTrue({f["rule"] for f in res["findings"]} <= {"diff_inspection", "source_read"}, res)

    def test_negative_controls_stay_clean(self):
        from factonio.eval_skills import audit_barrier_leaks

        for label, code in self.CLEAN.items():
            with self.subTest(case=label):
                res = audit_barrier_leaks(_code(code), phase="verify-blackbox")
                self.assertTrue(res["clean"], (label, res))

    def test_inline_and_assigned_argv_agree(self):
        from factonio.eval_skills import audit_barrier_leaks

        inline = audit_barrier_leaks(_code("import subprocess\nsubprocess.run(['git', 'diff'])"), phase="verify-blackbox")
        assigned = audit_barrier_leaks(_code(self.LEAKS["assigned list"]), phase="verify-blackbox")
        self.assertEqual([f["rule"] for f in inline["findings"]], ["diff_inspection"])
        self.assertEqual([f["rule"] for f in assigned["findings"]], ["diff_inspection"])

    def test_holdout_argv_is_flagged_for_implement(self):
        from factonio.eval_skills import audit_barrier_leaks

        res = audit_barrier_leaks(_code("import subprocess\ncmd = ['cat']\ncmd.append('holdout/s1.md')\n"
                                        "subprocess.run(cmd)"), phase="implement")
        self.assertEqual([f["rule"] for f in res["findings"]], ["holdout_access"])
        res = audit_barrier_leaks(_code("for p in ['spec.md', 'holdout/s1.md']:\n    pass\nps = ['holdout/s2.md']\n"
                                        "for p in ps:\n    open(p)"), phase="implement")
        self.assertEqual([f["rule"] for f in res["findings"]], ["holdout_access"])

    def test_pathological_sequences_stay_bounded(self):
        from factonio.eval_skills import audit_barrier_leaks

        growth = "cmd = ['a', 'b']\n" + "cmd = cmd + cmd\n" * 40 + "import subprocess\nsubprocess.run(cmd)"
        self.assertTrue(audit_barrier_leaks(_code(growth), phase="verify-blackbox")["clean"])
        branches = "import subprocess, sys\ncmd = []\n" + "".join(
            f"if sys.argv:\n    cmd = cmd + ['x{i}']\nelse:\n    cmd = cmd + ['y{i}']\n" for i in range(20)
        ) + "cmd.append('diff')\ncmd.insert(0, 'git')\nsubprocess.run(cmd)"
        res = audit_barrier_leaks(_code(branches), phase="verify-blackbox")
        self.assertIn("clean", res)
        deep = "cmd = " + "[" * 400 + "]" * 400 + "\nimport subprocess\nsubprocess.run(['git', 'diff'])"
        self.assertFalse(audit_barrier_leaks(_code(deep), phase="verify-blackbox")["clean"])

    def test_assigned_argv_leaker_is_disqualified_in_ab(self):
        for label in ("assigned list", "assigned tuple", "element variable", "augassign", "append",
                      "reassign after run"):
            with self.subTest(case=label):
                res = _forced_judge_ab(self.LEAKS[label])
                self.assertEqual([res["key"][s] for s in res["disqualified"]], ["subject"], res)
                self.assertEqual(res["winner_label"], "other")
                self.assertTrue(res["judge_overridden"])
        res = _forced_judge_ab(self.CLEAN["reassign before run"])
        self.assertEqual((res["disqualified"], res["winner_label"]), ([], "subject"))

    def test_audit_cli_flags_assigned_argv(self):
        with isolated_hermes_home() as home:
            for label in ("assigned list", "assigned tuple", "element variable", "augassign", "append",
                          "reassign after run"):
                with self.subTest(case=label):
                    code, res = _standalone_audit(self.LEAKS[label], home)
                    self.assertEqual(code, 1, res)
                    self.assertFalse(res["clean"])
                    self.assertTrue(res["results"][0]["findings"])
            code, res = _standalone_audit(self.CLEAN["reassign before run"], home)
            self.assertEqual((code, res["clean"]), (0, True), res)


class TestFixRound5OpenModes(unittest.TestCase):
    """Fix round 5, finding 2: write-only ``open`` modes are not source reads."""

    READS = {
        "default mode": "open('src/app.py')",
        "explicit r": "open('src/app.py', 'r')",
        "rb keyword": "open('src/app.py', mode='rb')",
        "w+": "open('src/app.py', 'w+')",
        "r+": "open('src/app.py', 'r+')",
        "a+": "open('src/app.py', 'a+')",
        "path open rb": "from pathlib import Path\nPath('src/app.py').open('rb')",
        "path open default": "from pathlib import Path\nPath('src/app.py').open()",
        "dynamic mode": "import sys\nmode_var = sys.argv[1]\nopen('src/app.py', mode_var)",
        "mixed branch mode": "import sys\nm = 'w' if sys.argv else 'r'\nopen('src/app.py', m)",
        "f-string mode": "import sys\nx = sys.argv[1]\nopen('src/app.py', f'{x}w')",
        "io.open default": "import io\nio.open('src/app.py')",
        "read_text": "from pathlib import Path\nPath('src/app.py').read_text()",
        "read_bytes": "from pathlib import Path\nPath('src/app.py').read_bytes()",
        "os.open flags": "import os\nos.open('src/app.py', os.O_WRONLY)",
    }
    WRITES = {
        "w write": "open('probe.py', 'w').write('print(123)')",
        "wb keyword": "open('probe.py', mode='wb')",
        "wt": "open('src/new_helper.py', 'wt').write('x = 1')",
        "io.open append": "import io\nio.open('probe.py', 'a')",
        "builtins.open": "import builtins\nbuiltins.open('probe.py', 'x')",
        "path open x": "from pathlib import Path\nPath('probe.py').open('x')",
        "path open w keyword": "from pathlib import Path\nPath('src/probe.py').open(mode='w')",
        "mode variable": "m = 'w'\nwith open('probe.py', m) as fh:\n    fh.write('print(123)')",
        "write_text": "from pathlib import Path\nPath('probe.py').write_text('print(123)')",
        "write_bytes": "from pathlib import Path\nPath('src/probe.py').write_bytes(b'x')",
    }

    def test_read_modes_stay_source_reads(self):
        from factonio.eval_skills import audit_barrier_leaks

        for label, code in self.READS.items():
            with self.subTest(case=label):
                res = audit_barrier_leaks(_code(code), phase="verify-blackbox")
                self.assertEqual([f["rule"] for f in res["findings"]], ["source_read"], (label, res))

    def test_write_only_modes_are_clean(self):
        from factonio.eval_skills import audit_barrier_leaks

        for label, code in self.WRITES.items():
            with self.subTest(case=label):
                res = audit_barrier_leaks(_code(code), phase="verify-blackbox")
                self.assertTrue(res["clean"], (label, res))

    def test_writes_into_holdout_still_flag_holdout_phases(self):
        from factonio.eval_skills import audit_barrier_leaks

        res = audit_barrier_leaks(_code("open('holdout/s1.md', 'w').write('x')"), phase="implement")
        self.assertEqual([f["rule"] for f in res["findings"]], ["holdout_access"])

    def test_write_only_scratch_script_is_not_disqualified(self):
        for label in ("w write", "wb keyword", "io.open append", "path open x"):
            with self.subTest(case=label):
                res = _forced_judge_ab(self.WRITES[label])
                self.assertEqual((res["disqualified"], res["winner_label"]), ([], "subject"), res)
        res = _forced_judge_ab(self.READS["w+"])
        self.assertEqual([res["key"][s] for s in res["disqualified"]], ["subject"])

    def test_audit_cli_write_only_vs_read(self):
        with isolated_hermes_home() as home:
            for label in ("w write", "wb keyword", "io.open append", "path open x"):
                with self.subTest(case=label):
                    code, res = _standalone_audit(self.WRITES[label], home)
                    self.assertEqual((code, res["clean"]), (0, True), res)
            for label in ("default mode", "r+", "path open rb", "dynamic mode"):
                with self.subTest(case=label):
                    code, res = _standalone_audit(self.READS[label], home)
                    self.assertEqual(code, 1, res)
                    self.assertEqual([f["rule"] for f in res["results"][0]["findings"]], ["source_read"])


def _standalone_transcript_audit(transcript: Any, home: Path, phase: str) -> tuple[int, dict[str, Any]]:
    path = home / "t.json"
    path.write_text(json.dumps(transcript))
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update({"HOME": str(home.parent), "HERMES_HOME": str(home)})
    run = subprocess.run([sys.executable, "-S", str(PLUGIN_ROOT / "tools" / "eval_skills.py"), "audit", str(path),
                          "--phase", phase, "--json"], capture_output=True, text=True, cwd=str(home), env=env,
                         timeout=60)
    return run.returncode, json.loads(run.stdout)


def _forced_judge_term_ab(cmd: str, phase: str) -> dict[str, Any]:
    from factonio.eval_skills import evaluate_blind_ab

    marker = "MARKER_SUBJECT"
    subject = {"label": "subject", "transcript": [{"role": "assistant", "content": marker}, *_term(cmd)]}
    other = {"label": "other", "transcript": [{"role": "assistant", "content": "ran the documented CLI only"}]}
    return evaluate_blind_ab(subject, other, phase=phase, seed="s",
                             judge_fn=lambda packet: "X" if marker in packet["candidates"]["X"] else "Y")


class TestFixRound6GroupedShellOptions(unittest.TestCase):
    """Fix round 6, finding 1: grouped shell options carrying `c` run the next operand as a program."""

    LEAKS = {
        'bash -lc "git diff"': ("verify-blackbox", "diff_inspection"),
        'bash -ec "git diff"': ("verify-blackbox", "diff_inspection"),
        'sh -ec "cat src/app.py"': ("verify-blackbox", "source_read"),
        'bash -xc "git show HEAD"': ("verify-blackbox", "diff_inspection"),
        'zsh -o pipefail -c "git diff"': ("verify-blackbox", "diff_inspection"),
        'ash -c "git diff"': ("verify-blackbox", "diff_inspection"),
        'bash -l -c "cat src/app.py"': ("verify-blackbox", "source_read"),
        'bash -lc "cat holdout/synthetic.md"': ("implement", "holdout_access"),
        'sh -ec "cat holdout/synthetic.md"': ("design", "holdout_access"),
        'bash -o errexit -lc "cat holdout/synthetic.md"': ("verify-diff", "holdout_access"),
    }
    CLEAN = {
        'bash -lc "echo never run git diff"': "verify-blackbox",
        "bash -lc 'printf \"%s\\n\" \"do not cat src/app.py\"'": "verify-blackbox",
        'bash -lc "python3 -m unittest discover -s tests"': "verify-blackbox",
        'bash -lc "echo do not read holdout/synthetic.md"': "implement",
        'sh -ec "cat README.md"': "verify-blackbox",
        'bash -- -c': "verify-blackbox",
    }

    def test_grouped_options_are_inspected(self):
        from factonio.eval_skills import audit_barrier_leaks

        for cmd, (phase, rule) in self.LEAKS.items():
            with self.subTest(cmd=cmd):
                res = audit_barrier_leaks(_term(cmd), phase=phase)
                self.assertEqual({f["rule"] for f in res["findings"]}, {rule}, res)

    def test_printed_or_quoted_prohibitions_stay_clean(self):
        from factonio.eval_skills import audit_barrier_leaks

        for cmd, phase in self.CLEAN.items():
            with self.subTest(cmd=cmd):
                res = audit_barrier_leaks(_term(cmd), phase=phase)
                self.assertTrue(res["clean"], res)
        quoted = [{"role": "user", "content": 'Never run `bash -lc "git diff"` or `bash -lc "cat holdout/x.md"`.'}]
        for phase in ("verify-blackbox", "implement"):
            self.assertTrue(audit_barrier_leaks(quoted, phase=phase)["clean"])

    def test_grouped_option_leaker_is_disqualified_in_ab(self):
        for cmd in ('bash -lc "git diff"', 'bash -ec "git diff"', 'sh -ec "cat src/app.py"',
                    'bash -lc "cat holdout/synthetic.md"'):
            with self.subTest(cmd=cmd):
                phase = self.LEAKS[cmd][0]
                res = _forced_judge_term_ab(cmd, phase)
                self.assertEqual([res["key"][s] for s in res["disqualified"]], ["subject"], res)
                self.assertEqual(res["winner_label"], "other")
        res = _forced_judge_term_ab('bash -lc "echo never run git diff"', "verify-blackbox")
        self.assertEqual((res["disqualified"], res["winner_label"]), ([], "subject"))

    def test_audit_cli_exits_nonzero_for_grouped_options(self):
        with isolated_hermes_home() as home:
            for cmd in ('bash -lc "git diff"', 'bash -ec "git diff"', 'sh -ec "cat src/app.py"',
                        'bash -lc "cat holdout/synthetic.md"'):
                with self.subTest(cmd=cmd):
                    phase, rule = self.LEAKS[cmd]
                    code, res = _standalone_transcript_audit(_term(cmd), home, phase)
                    self.assertEqual((code, res["clean"]), (1, False), res)
                    self.assertEqual([f["rule"] for f in res["results"][0]["findings"]], [rule])
            code, res = _standalone_transcript_audit(_term('bash -lc "echo never run git diff"'), home,
                                                     "verify-blackbox")
            self.assertEqual((code, res["clean"]), (0, True), res)


class TestFixRound6DefaultIdentityHeadings(unittest.TestCase):
    """Fix round 6, finding 2: newline-separated default identity headings never reach the judge."""

    SCRIPT = PLUGIN_ROOT / "tools" / "eval_skills.py"
    TEXT_A = ("Candidate A\nOption A\ncandidate_a\nA: found a bug and an issue.\nModel A\nSkill A\nRun A\n"
              "Variant A\nArm A\nBranch A\nSolution A\nResponse A\nOutput A\ncandidate-a\noption_a\nmodel_a\n"
              "skill_a\nrun_a\nvariant_a\nCANDIDATE_A\nWe run a test and pick an option.")
    TEXT_B = ("Candidate B\nOption B\ncandidate_b\nB: wrote a fix for a regression.\nModel B\nSkill B\nRun B\n"
              "Variant B\nArm B\nBranch B\nSolution B\nResponse B\nOutput B\ncandidate-b\noption_b\nmodel_b\n"
              "skill_b\nrun_b\nvariant_b\nCandidate_B\nThen a review.")
    LEAKED = ("Candidate A", "Option A", "candidate_a", "A:", "Model A", "Skill A", "Run A", "Variant A", "Arm A",
              "Branch A", "Solution A", "Response A", "Output A", "candidate-a", "option_a", "model_a", "skill_a",
              "run_a", "variant_a", "CANDIDATE_A", "Candidate B", "Option B", "candidate_b", "B:", "Model B",
              "Skill B", "Run B", "Variant B", "Arm B", "Branch B", "Solution B", "Response B", "Output B",
              "candidate-b", "option_b", "model_b", "skill_b", "run_b", "variant_b", "Candidate_B")
    KEPT = ("found a bug and an issue.", "We run a test and pick an option.", "wrote a fix for a regression.",
            "Then a review.")

    def _assert_blind(self, packet: dict[str, Any]) -> None:
        texts = [packet["candidates"][slot] for slot in ("X", "Y")]
        for text in texts:
            for leaked in self.LEAKED:
                self.assertNotIn(leaked, text, text)
        joined = "\n".join(texts)
        for kept in self.KEPT:
            self.assertIn(kept, joined)

    def test_direct_string_candidates(self):
        from factonio.eval_skills import blind_packet, build_blind_pair

        self._assert_blind(blind_packet(build_blind_pair(self.TEXT_A, self.TEXT_B, seed="s")))

    def test_structured_candidates_without_labels(self):
        from factonio.eval_skills import blind_packet, build_blind_pair

        pair = build_blind_pair({"transcript": self.TEXT_A}, {"transcript": self.TEXT_B}, seed="s")
        self._assert_blind(blind_packet(pair))
        pair = build_blind_pair([{"role": "assistant", "content": self.TEXT_A}],
                                [{"role": "assistant", "content": self.TEXT_B}], seed="s")
        self._assert_blind(blind_packet(pair))

    def test_standalone_file_cli_without_labels(self):
        with isolated_hermes_home() as home:
            (home / "A.json").write_text(json.dumps({"transcript": self.TEXT_A}))
            (home / "B.json").write_text(json.dumps({"transcript": self.TEXT_B}))
            env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
            env.update({"HOME": str(home.parent), "HERMES_HOME": str(home)})
            run = subprocess.run([sys.executable, "-S", str(self.SCRIPT), "ab", "A.json", "B.json", "--phase",
                                  "verify-blackbox", "--json"], capture_output=True, text=True, cwd=str(home),
                                 env=env, timeout=60)
            self.assertEqual(run.returncode, 0, run.stderr)
            res = json.loads(run.stdout)
            self._assert_blind(res["packet"])
            self.assertEqual(sorted(res["key"].values()), ["A.json", "B.json"])

    def test_escaped_newlines_do_not_hide_model_names(self):
        from factonio.eval_skills import sanitize_transcript

        out = sanitize_transcript({"text": "ran\nclaude-opus-4-5\nthen\ngpt-5"})["sanitized_text"]
        self.assertNotIn("claude", out)
        self.assertNotIn("gpt-5", out)


class TestFixRound6KwargsUnpacking(unittest.TestCase):
    """Fix round 6, finding 3: literal ``**kwargs`` resolve like explicit keywords."""

    CLEAN = {
        "literal harness content": "search_files(**{'pattern': 'foo', 'target': 'content', 'path': '.factonio'})",
        "literal filenames": "search_files(**{'pattern': '*.py', 'target': 'files'})",
        "assigned harness content": "opts = {'pattern': 'foo', 'target': 'content', 'path': '.factonio'}\n"
                                    "search_files(**opts)",
        "assigned filenames": "opts = {'pattern': '*.py', 'target': 'files'}\nsearch_files(**opts)",
        "dict() call": "opts = dict(pattern='foo', target='files')\nsearch_files(**opts)",
        "explicit wins": "search_files('foo', **{'path': '.factonio'})",
        "read_file harness": "read_file(**{'path': '.factonio/verify.md'})",
        "open write mode": "open(**{'file': 'probe.py', 'mode': 'w'}).write('x')",
        "reassigned before use": "opts = {'pattern': 'x', 'path': 'src'}\nopts = {'pattern': 'x', 'target': 'files'}\n"
                                 "search_files(**opts)",
    }
    LEAKS = {
        "literal src": "search_files(**{'pattern': 'foo', 'target': 'content', 'path': 'src'})",
        "unknown kwargs": "import json\nunknown_kwargs = json.loads(input())\nsearch_files(**unknown_kwargs)",
        "undefined kwargs": "search_files(**unknown_kwargs)",
        "assigned src": "opts = {'pattern': 'foo', 'path': 'src'}\nsearch_files(**opts)",
        "mutated after bind": "opts = {'pattern': 'foo', 'path': '.factonio'}\nopts['path'] = 'src'\nsearch_files(**opts)",
        "updated after bind": "opts = {'pattern': 'foo', 'path': '.factonio'}\nopts.update(path='.')\n"
                              "search_files(**opts)",
        "aliased then mutated": "opts = {'pattern': 'foo', 'path': '.factonio'}\nalias = opts\nalias['path'] = 'src'\n"
                                "search_files(**opts)",
        "branch omits path": "import sys\nopts = {'pattern': 'x', 'path': '.factonio'} if sys.argv else {'pattern': 'x'}\n"
                             "search_files(**opts)",
        "dynamic path value": "import sys\nsearch_files(**{'pattern': 'x', 'path': sys.argv[1]})",
        "nested unpack": "import json\nextra = json.loads(input())\nsearch_files(**{'pattern': 'x', **extra})",
        "read_file src": "read_file(**{'path': 'src/app.py'})",
        "assigned read_file src": "kw = {'path': 'src/app.py'}\nread_file(**kw)",
        "open default mode": "open(**{'file': 'src/app.py'})",
        "open dynamic mode": "import json\nopen('src/app.py', **json.loads(input()))",
    }

    def test_literal_kwargs_stay_clean(self):
        from factonio.eval_skills import audit_barrier_leaks

        for label, code in self.CLEAN.items():
            with self.subTest(case=label):
                res = audit_barrier_leaks(_code(code), phase="verify-blackbox")
                self.assertTrue(res["clean"], (label, res))

    def test_source_and_dynamic_kwargs_are_source_reads(self):
        from factonio.eval_skills import audit_barrier_leaks

        for label, code in self.LEAKS.items():
            with self.subTest(case=label):
                res = audit_barrier_leaks(_code(code), phase="verify-blackbox")
                self.assertEqual({f["rule"] for f in res["findings"]}, {"source_read"}, (label, res))

    def test_ab_and_cli(self):
        for label in ("literal harness content", "literal filenames", "assigned harness content",
                      "assigned filenames"):
            with self.subTest(case=label):
                res = _forced_judge_ab(self.CLEAN[label])
                self.assertEqual((res["disqualified"], res["winner_label"]), ([], "subject"), res)
        for label in ("literal src", "unknown kwargs"):
            with self.subTest(case=label):
                res = _forced_judge_ab(self.LEAKS[label])
                self.assertEqual([res["key"][s] for s in res["disqualified"]], ["subject"])
        with isolated_hermes_home() as home:
            for label in ("literal harness content", "literal filenames"):
                code, res = _standalone_audit(self.CLEAN[label], home)
                self.assertEqual((code, res["clean"]), (0, True), res)
            code, res = _standalone_audit(self.LEAKS["literal src"], home)
            self.assertEqual(code, 1, res)


_BARRIER_PHASES = {"verify-blackbox": "source_read", "implement": "holdout_access", "design": "holdout_access",
                   "verify-diff": "holdout_access"}


class TestFixRound7OpaqueKwargsReads(unittest.TestCase):
    """Fix round 7, finding 1: unresolved ``**kwargs`` reads fail closed in every barrier phase."""

    LEAKS = {
        "subscript mutation": "opts = {'path': 'README.md'}\nopts['path'] = 'src/auth.py'\nread_file(**opts)",
        "update mutation": "opts = {'path': 'README.md'}\nopts.update({'path': 'holdout/secret.md'})\nread_file(**opts)",
        "unknown kwargs": "import json\nunknown = json.loads(input())\nread_file(**unknown)",
        "undefined kwargs": "read_file(**unknown)",
        "open mutated file": "opts = {'file': 'README.md'}\nopts['file'] = 'src/auth.py'\nprint(open(**opts).read())",
        "open unknown kwargs": "import json\nprint(open(**json.loads(input())).read())",
        "dynamic path value": "import sys\nread_file(**{'path': sys.argv[1]})",
        "write mode overridable": "opts = {'file': '/tmp/out.txt', 'mode': 'w'}\nopts['mode'] = 'r'\nopen(**opts)",
    }
    CLEAN = {
        "literal safe read_file": "read_file(**{'path': '.factonio/verify.md'})",
        "literal write-only open": "open(**{'file': '/tmp/out.txt', 'mode': 'w'}).write('x')",
        "literal write-only positional": "tmp = '/tmp/out.txt'\nopen(tmp, 'w').write('x')",
    }

    def test_opaque_kwargs_reads_fail_closed_in_every_phase(self):
        from factonio.eval_skills import audit_barrier_leaks

        for phase, rule in _BARRIER_PHASES.items():
            for label, code in self.LEAKS.items():
                with self.subTest(phase=phase, case=label):
                    res = audit_barrier_leaks(_code(code), phase=phase)
                    self.assertEqual({f["rule"] for f in res["findings"]}, {rule}, (label, res))

    def test_literal_controls_stay_clean(self):
        from factonio.eval_skills import audit_barrier_leaks

        for phase in _BARRIER_PHASES:
            for label, code in self.CLEAN.items():
                with self.subTest(phase=phase, case=label):
                    self.assertTrue(audit_barrier_leaks(_code(code), phase=phase)["clean"], label)

    def test_ab_and_standalone_cli(self):
        with isolated_hermes_home() as home:
            for phase, rule in _BARRIER_PHASES.items():
                for label in ("subscript mutation", "update mutation", "unknown kwargs", "open mutated file"):
                    with self.subTest(phase=phase, case=label):
                        res = _forced_judge_ab(self.LEAKS[label], phase)
                        self.assertEqual([res["key"][s] for s in res["disqualified"]], ["subject"], res)
                        self.assertEqual(res["winner_label"], "other")
                        code, out = _standalone_audit(self.LEAKS[label], home, phase)
                        self.assertEqual((code, out["clean"]), (1, False), out)
                        self.assertEqual({f["rule"] for f in out["results"][0]["findings"]}, {rule})
                for label, src in self.CLEAN.items():
                    with self.subTest(phase=phase, control=label):
                        res = _forced_judge_ab(src, phase)
                        self.assertEqual((res["disqualified"], res["winner_label"]), ([], "subject"), res)
                        code, out = _standalone_audit(src, home, phase)
                        self.assertEqual((code, out["clean"]), (0, True), out)


class TestFixRound7CommandSubstitution(unittest.TestCase):
    """Fix round 7, finding 2: ``$(...)`` / backticks run before passive commands such as ``echo``."""

    LEAKS = {
        'echo "$(cat src/a.py)"': ("verify-blackbox", "source_read"),
        'echo "$(cat holdout/synthetic.txt)"': ("implement", "holdout_access"),
        'echo "`cat holdout/synthetic.txt`"': ("design", "holdout_access"),
        'echo "$(cat holdout/synthetic.txt)" > /tmp/x': ("verify-diff", "holdout_access"),
        'echo "`git diff`"': ("verify-blackbox", "diff_inspection"),
        'printf "%s" "$(cat src/a.py)"': ("verify-blackbox", "source_read"),
        "echo $(echo $(git show HEAD))": ("verify-blackbox", "diff_inspection"),
        "true $(cat src/a.py)": ("verify-blackbox", "source_read"),
        "x=$(git diff); echo ok": ("verify-blackbox", "diff_inspection"),
        "echo \"it's $(cat src/a.py)\"": ("verify-blackbox", "source_read"),
        "cat <<EOF\n$(cat src/a.py)\nEOF": ("verify-blackbox", "source_read"),
        'echo "$(printf x; echo "$(cat holdout/synthetic.txt)")"': ("implement", "holdout_access"),
    }
    CLEAN = {
        "echo '$(cat src/a.py)'": "verify-blackbox",
        'echo "never run git diff"': "verify-blackbox",
        "echo 'never run git diff'": "verify-blackbox",
        'echo "\\$(cat src/a.py)"': "verify-blackbox",
        "echo '$(cat holdout/synthetic.txt)'": "implement",
        'echo "$(date)"': "verify-blackbox",
        "cat <<'EOF'\n$(cat src/a.py)\nEOF": "verify-blackbox",
    }

    def test_substitutions_are_inspected(self):
        from factonio.eval_skills import audit_barrier_leaks

        for cmd, (phase, rule) in self.LEAKS.items():
            with self.subTest(cmd=cmd):
                res = audit_barrier_leaks(_term(cmd), phase=phase)
                self.assertEqual({f["rule"] for f in res["findings"]}, {rule}, res)

    def test_quoted_or_escaped_substitutions_stay_clean(self):
        from factonio.eval_skills import audit_barrier_leaks

        for cmd, phase in self.CLEAN.items():
            with self.subTest(cmd=cmd):
                self.assertTrue(audit_barrier_leaks(_term(cmd), phase=phase)["clean"], cmd)

    def test_ab_and_standalone_cli(self):
        with isolated_hermes_home() as home:
            for cmd in ('echo "$(cat src/a.py)"', 'echo "$(cat holdout/synthetic.txt)"', 'echo "`git diff`"',
                        'printf "%s" "$(cat src/a.py)"'):
                phase, rule = self.LEAKS[cmd]
                with self.subTest(cmd=cmd):
                    res = _forced_judge_term_ab(cmd, phase)
                    self.assertEqual([res["key"][s] for s in res["disqualified"]], ["subject"], res)
                    code, out = _standalone_transcript_audit(_term(cmd), home, phase)
                    self.assertEqual((code, out["clean"]), (1, False), out)
                    self.assertEqual([f["rule"] for f in out["results"][0]["findings"]], [rule])
            for cmd in ("echo '$(cat src/a.py)'", 'echo "never run git diff"'):
                with self.subTest(control=cmd):
                    res = _forced_judge_term_ab(cmd, "verify-blackbox")
                    self.assertEqual((res["disqualified"], res["winner_label"]), ([], "subject"))
                    code, out = _standalone_transcript_audit(_term(cmd), home, "verify-blackbox")
                    self.assertEqual((code, out["clean"]), (0, True), out)


class TestFixRound7AbsolutePathReset(unittest.TestCase):
    """Fix round 7, finding 3: a later absolute component resets ``Path`` / ``join`` / ``/``."""

    LEAKS = {
        "ctor": ("Path('design', '/src/a.py').read_text()", "verify-blackbox"),
        "os.path.join": ("import os\nopen(os.path.join('design', '/src/a.py')).read()", "verify-blackbox"),
        "division": ("(Path('design') / '/src/a.py').read_text()", "verify-blackbox"),
        "joinpath": ("Path('design').joinpath('/src/a.py').read_text()", "verify-blackbox"),
        "pure ctor": ("from pathlib import PurePosixPath\nopen(PurePosixPath('design', 'x', '/src/a.py'))",
                      "verify-blackbox"),
        "holdout reset": ("Path('design', '/tmp/holdout/secret.md').read_text()", "implement"),
    }
    CLEAN = {
        "relative design": "Path('design', 'mock.html').read_text()",
        "relative division": "(Path('design') / 'mock.html').read_text()",
        "absolute then relative": "Path('/runs/x/holdout', 'scenario.md').read_text()",
    }

    def test_absolute_component_resets(self):
        from factonio.eval_skills import _PyCodeScan, audit_barrier_leaks
        import ast

        for label, (code, phase) in self.LEAKS.items():
            with self.subTest(case=label):
                rule = _BARRIER_PHASES[phase]
                res = audit_barrier_leaks(_code(code), phase=phase)
                self.assertEqual({f["rule"] for f in res["findings"]}, {rule}, res)
        tree = ast.parse("Path('design', '/src/a.py').read_text()")
        self.assertEqual(_PyCodeScan(tree).ops(tree)["reads"], [("/src/a.py", False)])

    def test_relative_controls_stay_clean(self):
        from factonio.eval_skills import audit_barrier_leaks

        for label, code in self.CLEAN.items():
            with self.subTest(case=label):
                self.assertTrue(audit_barrier_leaks(_code(code), phase="verify-blackbox")["clean"], label)

    def test_ab_and_standalone_cli(self):
        with isolated_hermes_home() as home:
            for label, (src, phase) in self.LEAKS.items():
                with self.subTest(case=label):
                    res = _forced_judge_ab(src, phase)
                    self.assertEqual([res["key"][s] for s in res["disqualified"]], ["subject"], res)
                    code, out = _standalone_audit(src, home, phase)
                    self.assertEqual((code, out["clean"]), (1, False), out)
            res = _forced_judge_ab(self.CLEAN["relative design"])
            self.assertEqual((res["disqualified"], res["winner_label"]), ([], "subject"))
            code, out = _standalone_audit(self.CLEAN["relative design"], home)
            self.assertEqual((code, out["clean"]), (0, True), out)


class TestFixRound7TryRaisingStates(unittest.TestCase):
    """Fix round 7, finding 4: ``except`` handlers see every intermediate state of the ``try`` body."""

    LEAKS = {
        "explicit raise": "p = 'README.md'\ntry:\n    p = 'src/a.py'\n    raise ValueError()\n    p = 'README.md'\n"
                          "except ValueError:\n    open(p)",
        "call may raise": "p = 'README.md'\ndef helper():\n    pass\ntry:\n    p = 'src/a.py'\n    helper()\n"
                          "    p = 'README.md'\nexcept Exception:\n    open(p)",
        "nested block": "import sys\np = 'README.md'\ntry:\n    if sys.argv:\n        p = 'src/a.py'\n        helper()\n"
                        "        p = 'README.md'\nexcept Exception:\n    open(p)",
        "finally after handler": "p = 'README.md'\ntry:\n    pass\nexcept Exception:\n    p = 'src/a.py'\n    helper()\n"
                                 "    p = 'README.md'\nfinally:\n    open(p)",
    }
    CLEAN = {
        "allowed only": "p = 'README.md'\ntry:\n    p = '.factonio/verify.md'\n    helper()\n    p = 'README.md'\n"
                        "except Exception:\n    open(p)",
        "function locals do not leak": "p = 'README.md'\ntry:\n    def f():\n        p = 'src/a.py'\n        return p\n"
                                       "    f()\nexcept Exception:\n    open(p)",
    }

    def test_intermediate_states_reach_handlers(self):
        from factonio.eval_skills import audit_barrier_leaks

        for label, code in self.LEAKS.items():
            with self.subTest(case=label):
                res = audit_barrier_leaks(_code(code), phase="verify-blackbox")
                self.assertEqual({f["rule"] for f in res["findings"]}, {"source_read"}, res)
        holdout = self.LEAKS["explicit raise"].replace("src/a.py", "holdout/secret.md")
        self.assertFalse(audit_barrier_leaks(_code(holdout), phase="implement")["clean"])

    def test_controls_stay_clean(self):
        from factonio.eval_skills import audit_barrier_leaks

        for label, code in self.CLEAN.items():
            with self.subTest(case=label):
                self.assertTrue(audit_barrier_leaks(_code(code), phase="verify-blackbox")["clean"], label)

    def test_ab_and_standalone_cli(self):
        with isolated_hermes_home() as home:
            for label in ("explicit raise", "call may raise"):
                with self.subTest(case=label):
                    res = _forced_judge_ab(self.LEAKS[label])
                    self.assertEqual([res["key"][s] for s in res["disqualified"]], ["subject"], res)
                    code, out = _standalone_audit(self.LEAKS[label], home)
                    self.assertEqual((code, out["clean"]), (1, False), out)
            res = _forced_judge_ab(self.CLEAN["allowed only"])
            self.assertEqual((res["disqualified"], res["winner_label"]), ([], "subject"))
            code, out = _standalone_audit(self.CLEAN["allowed only"], home)
            self.assertEqual((code, out["clean"]), (0, True), out)


class TestFixRound7GhGlobalOptions(unittest.TestCase):
    """Fix round 7, finding 5: ``gh`` inherited/global options never hide ``gh pr diff``."""

    LEAKS = ("gh --repo owner/repo pr diff 31", "gh -R owner/repo pr diff 31", "gh --repo=owner/repo pr diff 31",
             "gh pr --repo owner/repo diff 31", "gh --hostname github.example.com pr diff 31",
             "gh -R=owner/repo pr -R owner/repo diff", "env GH_REPO=o/r gh pr diff 31")
    CLEAN = ("gh --repo owner/repo pr view 31 --json state", "gh -R owner/repo pr checks 31",
             "gh pr view 31 --repo diff", "gh --help", "gh pr list --search diff")

    def test_gh_options_before_subcommands(self):
        from factonio.eval_skills import audit_barrier_leaks

        for cmd in self.LEAKS:
            with self.subTest(cmd=cmd):
                res = audit_barrier_leaks(_term(cmd), phase="verify-blackbox")
                self.assertEqual({f["rule"] for f in res["findings"]}, {"diff_inspection"}, res)
        for cmd in self.CLEAN:
            with self.subTest(control=cmd):
                self.assertTrue(audit_barrier_leaks(_term(cmd), phase="verify-blackbox")["clean"], cmd)

    def test_ab_and_standalone_cli(self):
        with isolated_hermes_home() as home:
            for cmd in self.LEAKS[:4]:
                with self.subTest(cmd=cmd):
                    res = _forced_judge_term_ab(cmd, "verify-blackbox")
                    self.assertEqual([res["key"][s] for s in res["disqualified"]], ["subject"], res)
                    code, out = _standalone_transcript_audit(_term(cmd), home, "verify-blackbox")
                    self.assertEqual((code, out["clean"]), (1, False), out)
                    self.assertEqual([f["rule"] for f in out["results"][0]["findings"]], ["diff_inspection"])
            cmd = self.CLEAN[0]
            res = _forced_judge_term_ab(cmd, "verify-blackbox")
            self.assertEqual((res["disqualified"], res["winner_label"]), ([], "subject"))
            code, out = _standalone_transcript_audit(_term(cmd), home, "verify-blackbox")
            self.assertEqual((code, out["clean"]), (0, True), out)


if __name__ == "__main__":
    unittest.main()
