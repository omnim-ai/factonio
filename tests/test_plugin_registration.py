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
