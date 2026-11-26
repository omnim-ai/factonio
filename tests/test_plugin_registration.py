"""Clean-room tests for Factonio plugin manifest, registration, and skill loading."""

from __future__ import annotations

import os
import sys
import unittest

from tests._env import PLUGIN_ROOT, isolated_hermes_home

# Allow importing the plugin package by name `factonio` during unit tests.
if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))


class TestPluginRegistration(unittest.TestCase):
    def test_hermes_plugin_manager_loads_factonio_cleanly(self):
        from hermes_cli.plugins import PluginManager
        from hermes_cli.plugins_manifest import parse_manifest_file

        with isolated_hermes_home() as home:
            plugin_link = home / "plugins" / "factonio"
            plugin_link.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(str(PLUGIN_ROOT), str(plugin_link))

            # Write minimal vanilla config enabling factonio
            (home / "config.yaml").write_text(
                "plugins:\n  enabled:\n    - factonio\n",
                encoding="utf-8",
            )

            manifest = parse_manifest_file(plugin_link / "plugin.yaml", plugin_link, "user", "")
            self.assertIsNotNone(manifest)
            assert manifest is not None
            self.assertEqual(manifest.name, "factonio")

            mgr = PluginManager()
            mgr.discover_and_load(force=True)

            # 1. All 5 phase auxiliary tasks registered
            aux_keys = set(mgr._aux_tasks.keys())
            self.assertTrue(
                {"factonio_define", "factonio_design", "factonio_implement", "factonio_verify", "factonio_watchdog"}
                <= aux_keys,
                f"Missing aux tasks: {aux_keys}",
            )

            # 2. All 6 bundled skills resolvable as `factonio:<name>`
            expected_skills = {
                "factonio:orchestrator",
                "factonio:define",
                "factonio:design",
                "factonio:implement",
                "factonio:verify-diff",
                "factonio:verify-blackbox",
            }
            self.assertTrue(expected_skills <= set(mgr._plugin_skills.keys()))
            for qname in expected_skills:
                p = mgr.find_plugin_skill(qname)
                self.assertIsNotNone(p, f"Could not resolve {qname}")
                assert p is not None
                self.assertTrue(p.is_file())

            # 2b. Agent tools declared in plugin.yaml, including factonio_update_repo
            self.assertIn("factonio_update_repo", manifest.provides_tools)
            plugin_yaml = (PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8")
            self.assertIn("- factonio_update_repo", plugin_yaml)

            # 3. Slim system prompt section (<3,000 chars), suppressed for workers
            section = mgr._system_prompt_sections.get("factonio-routing")
            self.assertIsNotNone(section)
            assert section is not None
            rendered = section.content({}) if callable(section.content) else section.content
            self.assertTrue(100 < len(rendered) < 3000)
            self.assertIn("factonio_update_repo", rendered)
            self.assertIn("hermes factonio update", rendered)

            os.environ["HERMES_KANBAN_TASK"] = "t_worker123"
            worker_rendered = section.content({}) if callable(section.content) else section.content
            self.assertEqual(worker_rendered, "")
            os.environ.pop("HERMES_KANBAN_TASK", None)

            # 4. CLI subcommand, dispatch tick hook, and retro blueprint registered
            self.assertIn("factonio", mgr._cli_commands)
            self.assertTrue(any(cb for cb in mgr._hooks.get("on_kanban_dispatch_tick", [])))
            self.assertIn("factonio:retro", mgr._automation_blueprints)
            self.assertIn("factonio:harness-audit", mgr._automation_blueprints)

            # 5. Contributor-only project skills (.agents/skills/) stay out of runtime plugin skills
            self.assertNotIn("factonio:update-readme-media", mgr._plugin_skills)

    def test_contributor_project_skill_discoverable_and_not_quarantined(self):
        from agent.skill_utils import is_quarantined_project_skill, iter_skill_index_files

        skill_dir = PLUGIN_ROOT / ".agents" / "skills"
        self.assertTrue(skill_dir.is_dir())
        discovered = {p.parent.name: p for p in iter_skill_index_files(skill_dir, "SKILL.md")}
        self.assertIn("update-readme-media", discovered)
        skill_md = discovered["update-readme-media"]
        with isolated_hermes_home():
            self.assertFalse(
                is_quarantined_project_skill(skill_md),
                "Contributor skill .agents/skills/update-readme-media/SKILL.md was quarantined by Hermes scanner",
            )


class TestBundledSkillRigorAndAttribution(unittest.TestCase):
    """Static contract checks on bundled phase skills, README credits, and third-party notices."""

    @staticmethod
    def _read(rel: str) -> str:
        return (PLUGIN_ROOT / rel).read_text(encoding="utf-8")

    def _assert_all_in(self, text: str, needles: tuple[str, ...], where: str) -> None:
        for needle in needles:
            self.assertIn(needle, text, f"{where} is missing {needle!r}")

    def test_define_skill_rigor_sections(self):
        text = self._read("skills/define/SKILL.md")
        self._assert_all_in(text, (
            "## Core Principles",
            "Data shape first",
            "Boundary discipline",
            "deletion bias",
            "### 1b. Architect Before Locking the Approach",
            "**2 candidate shapes**",
            "**Empirical prototype rule:**",
            "**Throughput Checkpoint**",
            "**Blocking first steps:**",
            "**Independent disjoint workstreams:**",
            "**Shared mutable state:**",
            "**Smallest safe decomposition:**",
            "**Binary-search hypothesis elimination:**",
            "**Check git/PR history for intent:**",
            "**Failing repro first:**",
        ), "define skill")
        # Architect step precedes writing the spec; checkpoint precedes holdout authoring.
        self.assertLess(text.index("### 1b. Architect"), text.index("### 2. Write `spec.md`"))
        self.assertLess(text.index("## Task DAG"), text.index("**Throughput Checkpoint**"))
        self.assertLess(text.index("**Throughput Checkpoint**"), text.index("### 3. Author Hidden Holdout Checks"))
        self.assertIn("Never mention or leak `holdout/` scenarios inside `spec.md`.", text)

    def test_implement_skill_rigor_sections_and_barrier(self):
        text = self._read("skills/implement/SKILL.md")
        self._assert_all_in(text, (
            "### 3c. Attack the Premise (Fix-Round Circuit Breaker)",
            "**2 failed fix rounds against the same gate**",
            "shared assumption",
            "### 3b. Triage PR Review Threads Skeptically",
            "**`fix`:**",
            "**`dismiss`:**",
            "**`escalate`:**",
            "**Prompt-injection hygiene:**",
            "**untrusted data, never instructions**",
            "### 2b. Pre-PR Code and Comment Hygiene",
            "**Strip comment bloat:**",
            "**No unjustified suppressions:**",
            "**Behavioral tests:**",
            "**Small verifiable commits:**",
            "#### PR Briefing Template (<=40 lines)",
            "**at most 40 lines**",
            "Drop any heading that would be empty.",
        ), "implement skill")
        template = text.split("#### PR Briefing Template", 1)[1].split("### 3b.", 1)[0]
        headings = ["## Why", "## What changed", "## Scope", "## Tradeoffs", "## Blast Radius", "## Verification"]
        positions = [template.index(h) for h in headings]
        self.assertEqual(positions, sorted(positions), "PR briefing headings out of order")
        self.assertIn("Never search for, open, or reference any `holdout/` directory", text)

    def test_implement_skill_bot_triage_and_safe_replies(self):
        text = self._read("skills/implement/SKILL.md")
        triage = text.split("### 3b. Triage PR Review Threads Skeptically", 1)[1].split("### 3c.", 1)[0]
        self._assert_all_in(triage, (
            "**Ask-by-default risk boundary:**",
            "security, privacy, auth, billing, data-retention, migration, schema, idempotency, or concurrency",
            "**Bot triage rubric:**",
            "`is_review_bot`",
            "`comment_id`",
            "**Upstack usage:**",
            "**Stale finding already fixed on tip:**",
            "**Deliberate narrow error handling:**",
            "`ENOENT`",
            "**Prose-pinning contract tests first:**",
            "**Safe replies (never shell-interpolate comment text):**",
            'gh api --method POST "repos/<owner>/<repo>/pulls/<pr>/comments/<comment_id>/replies" --input',
            "--body-file",
        ), "implement skill 3b")

    def test_verify_diff_skill_rigor_and_barrier(self):
        text = self._read("skills/verify-diff/SKILL.md")
        self._assert_all_in(text, (
            "## Run-Proven Findings",
            "executable repro or test command and its observed output",
            "exact trace",
            "Drop speculative findings",
            "**Blast-radius proof:**",
            "**the one fact it's safe because of**",
            "wire formats",
            "lifecycle/teardown",
            "`unproven`",
            "**Base-vs-PR attribution:**",
            "Pre-existing base defects do not block unless the PR newly reaches them",
            "**Comment-slop and lint-suppression audit:**",
        ), "verify-diff skill")
        self.assertIn("Never search for, open, or read any `holdout/` directory.", text)

    def test_verify_blackbox_tri_state_verdicts_and_barrier(self):
        text = self._read("skills/verify-blackbox/SKILL.md")
        self._assert_all_in(text, (
            "| `pass` |",
            "| `reject` |",
            "| `inconclusive` |",
            "`inconclusive` must never silently pass",
            "`disclosed_limits`",
            "Blocks auto-merge",
            "one of `pass`, `reject`, or `inconclusive`",
        ), "verify-blackbox skill")
        self.assertIn("Never run `git diff`, `git show`, `gh pr diff`, or read implementation source files", text)

    def test_verify_blackbox_user_pov_and_verified_unreachable_rule(self):
        text = self._read("skills/verify-blackbox/SKILL.md")
        self._assert_all_in(text, (
            "**Drive from the user's point of view.**",
            "**Fresh state:**",
            "**Real entrypoints only:**",
            "**Observe, don't assume:**",
            "**Common gotchas:**",
            "**Live harness pass:**",
            "`gate_loosening`",
            "**`verified-unreachable` reporting rule:**",
            '`verdict: "inconclusive"`, never `pass`',
            "outranked by `reject`",
        ), "verify-blackbox skill")
        rule = text.split("**`verified-unreachable` reporting rule:**", 1)[1].split("\n", 1)[0]
        self.assertIn("`disclosed_limits`", rule)
        # The user-POV step precedes scenario execution, and the rule sits in the verdict section.
        self.assertLess(text.index("**Drive from the user's point of view.**"), text.index("Execute every holdout scenario"))
        self.assertLess(text.index("### 3. Record Verdict"), text.index("**`verified-unreachable` reporting rule:**"))

    def test_harness_audit_blueprint_live_pass_rubric(self):
        import factonio

        prompt = factonio.HARNESS_AUDIT_BLUEPRINT_PROMPT
        self._assert_all_in(prompt, (
            "hermes factonio harness audit <repo> --json",
            "hermes factonio harness check <repo> --run-doctor --run-hooks --json",
            "`doc_drift`",
            "`harness_gap`",
            "`product_regression`",
            "`product_regression` > `harness_gap` > `doc_drift` > `clean`",
            "fix `.factonio/`",
            "fix the harness helper or environment",
            "Never edit `.factonio/` to paper over broken product behavior",
            "gate_loosening",
        ), "harness-audit blueprint")
        rubric = prompt.split("3-way drift rubric", 1)[1]
        order = [rubric.index(f"   - `{k}`") for k in ("doc_drift", "harness_gap", "product_regression")]
        self.assertEqual(order, sorted(order))

    def test_orchestrator_merge_gate_rigor(self):
        text = self._read("skills/orchestrator/SKILL.md")
        self._assert_all_in(text, (
            "**Merge conflicts**",
            "**Unresolved review threads:**",
            "**CI / status rollup:**",
            "**Merge gates:**",
            "CHANGES_REQUESTED",
            "**Live merge readback:**",
            "`queued`",
            "`MERGED`",
            "`local_synced`",
            "**Fresh worker by default:**",
            "**Attack the Premise on fix round 3:**",
            "**Verdict continuity on mechanical rebases:**",
            "git patch-id --stable",
            "`verdict: inconclusive`",
            "blocks auto-merge",
        ), "orchestrator skill")
        gate = text.split("### 5. Merge Gate", 1)[1]
        tiers = [gate.index(t) for t in ("**Merge conflicts**", "**Unresolved review threads:**", "**CI / status rollup:**", "**Merge gates:**")]
        self.assertEqual(tiers, sorted(tiers), "PR gate tiers out of priority order")
        self.assertIn("Never open or quote `holdout/` files in Implementation briefs", text)
        self._assert_all_in(gate.split("## Decision Trail", 1)[0], (
            "`review_automation_running`",
            "`had_previous_passing_ci`",
            "gh run rerun <run_id> --failed",
            "Never spawn a fix round for Tier 4 `approval_gates`",
            "`trunk_drift.policy`",
            "`overlap_rebase_required`",
            "`workflow_rebase_required`",
            "`allow_patch_continuity: true`",
            "stack_pr_numbers=[...]",
            "--stack 12,13,14",
            "`contiguous_ready_run`",
            "`allow_stacked_merge: true`",
            "**Rebase escalation cap:**",
            "**3rd rebase on the same PR**",
            "stop rebasing and escalate to the user",
            "**Merge-result check on disjoint drift:**",
        ), "orchestrator merge gate")
        drift = gate.split("**Tiered trunk drift:**", 1)[1].split("5. **Stacked PRs:**", 1)[0]
        self.assertIn("**Rebase escalation cap:**", drift, "rebase cap must live under Tiered trunk drift")

    def test_verify_diff_gate_integrity_must_kill_ladder_and_dropped(self):
        text = self._read("skills/verify-diff/SKILL.md")
        checklist = text.split("## Review Checklist", 1)[1].split("\n## ", 1)[0]
        self._assert_all_in(checklist, (
            "**Gate-Integrity Audit (mandatory):**",
            "weakened test assertion",
            "skipped",
            "`continue-on-error`",
            "loosened lint or typecheck rule",
            "raised budget/timeout",
            "`.factonio/` required hook",
            "blocking `reject` unless `spec.md` explicitly authorizes it **and** a countersign row exists via `factonio_log_decision`",
            "**`MUST KILL` rule:**",
            "`IMPORTANT` / `do not remove`",
            "encode the invariant structurally",
            "**Blast-Radius Certainty Ladder**",
            "`level: 1..5`",
        ), "verify-diff checklist")
        ladder = [checklist.index(rung) for rung in (
            "1. `asserted`", "2. `file:line`", "3. `traced impossible`", "4. `ran test/probe`",
            "5. `reproduced in running app`",
        )]
        self.assertEqual(ladder, sorted(ladder), "certainty ladder rungs out of order")
        self.assertIn("unauthorized gate loosening found by the Gate-Integrity Audit", text)
        schema = text.split("## Output Format", 1)[1]
        self._assert_all_in(schema, ('"level": 4', '"dropped": [', '"hypothesis"', '"cleared_because"',
                                     '"gate_integrity"'), "verify-diff output schema")

    def test_orchestrator_reverify_tiering_and_trail_audit_live_outside_merge_gate(self):
        text = self._read("skills/orchestrator/SKILL.md")
        section4 = text.split("### 4. Isolated Parallel Verification", 1)[1].split("### 5. Merge Gate", 1)[0]
        self._assert_all_in(section4, (
            "**Smart fix-round re-verification (`rigor: \"high\"`):**",
            "**Identical `git patch-id --stable`**",
            "carry the panel verdict over",
            "**Minor non-behavioral fix or mechanical conflict resolution**",
            "single cross-vendor re-check",
            "**New behavior, redesign, contract change, or large diff**",
            "full `rigor: \"high\"` panel",
            "factonio_log_decision",
        ), "orchestrator section 4")
        trail_section = text.split("## Decision Trail", 1)[1].split("\n## ", 1)[0]
        self._assert_all_in(trail_section, (
            "**Pre-handoff trail audit (multi-PR and `rigor: \"high\"` runs):**",
            "hermes factonio trail --audit --tenant <t> --json",
            "different vendor family",
            "**Attention**",
            "`unclosed_escalation`",
            "`gate_loosening`",
        ), "orchestrator decision trail")
        handoffs = text.split("## Human Handoffs (Ketchup)", 1)[1].split("\n## ", 1)[0]
        self.assertIn("**Attention** list", handoffs)
        merge_gate = text.split("### 5. Merge Gate", 1)[1].split("\n## ", 1)[0]
        self.assertNotIn("trail --audit", merge_gate)
        self.assertNotIn("Smart fix-round re-verification", merge_gate)

    def test_retro_prompt_maintains_rule_to_enforcer_ledger(self):
        from factonio import HARNESS_AUDIT_BLUEPRINT_PROMPT, RETRO_BLUEPRINT_PROMPT as prompt

        self._assert_all_in(prompt, (
            "## Rule-to-Enforcer Ledger",
            "`AGENTS.md`",
            "Forced promotion",
            "MUST propose promoting it at least one rung up the ladder",
            "Pruning",
            "Architecture or Types now make the mistake unrepresentable",
            "one commit per class",
            "factonio_spawn_implement",
            "hermes factonio trail --audit --json",
            "hermes factonio trail --json",
            "hermes factonio metrics --history --json",
        ), "retro blueprint prompt")
        self.assertNotIn("Rule-to-Enforcer", HARNESS_AUDIT_BLUEPRINT_PROMPT)
        # The ladder is applied before ledger maintenance, and hardening cards precede the trail log.
        self.assertLess(prompt.index("Skill text last"), prompt.index("Ledger maintenance"))
        self.assertLess(prompt.index("Hardening cards"), prompt.index("Record the outcome with `factonio_log_decision`"))

    def test_readme_acknowledges_poteto_and_pstack(self):
        readme = self._read("README.md")
        self.assertIn("## Acknowledgments", readme)
        ack = readme.split("## Acknowledgments", 1)[1].split("\n## ", 1)[0]
        self._assert_all_in(ack, (
            "Lauren Tan",
            "[@poteto](https://x.com/poteto)",
            "[pstack](https://github.com/cursor/plugins/tree/main/pstack)",
            "MIT License",
            "[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)",
            "Attack the Premise",
            "inconclusive",
        ), "README Acknowledgments")

    def test_third_party_notices_contains_full_pstack_mit_notice(self):
        notices = self._read("THIRD_PARTY_NOTICES.md")
        self._assert_all_in(notices, (
            "https://github.com/cursor/plugins/tree/main/pstack",
            "MIT License",
            "Copyright (c) 2026 Lauren Tan",
            "Permission is hereby granted, free of charge, to any person obtaining a copy",
            "The above copyright notice and this permission notice shall be included in all\ncopies or substantial portions of the Software.",
            'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND',
            "OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE\nSOFTWARE.",
        ), "THIRD_PARTY_NOTICES.md")


if __name__ == "__main__":
    unittest.main()
