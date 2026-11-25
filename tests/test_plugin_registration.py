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


if __name__ == "__main__":
    unittest.main()
