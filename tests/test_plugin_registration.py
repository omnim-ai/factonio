"""Clean-room tests for Factonio plugin manifest, registration, and skill loading."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

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

            # 1. All 4 phase auxiliary tasks registered
            aux_keys = set(mgr._aux_tasks.keys())
            self.assertTrue(
                {"factonio_define", "factonio_implement", "factonio_verify", "factonio_watchdog"} <= aux_keys,
                f"Missing aux tasks: {aux_keys}",
            )

            # 2. All 5 bundled skills resolvable as `factonio:<name>`
            expected_skills = {
                "factonio:orchestrator",
                "factonio:define",
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

            # 3. Slim system prompt section (<3,000 chars), suppressed for workers
            section = mgr._system_prompt_sections.get("factonio-routing")
            self.assertIsNotNone(section)
            assert section is not None
            rendered = section.content({}) if callable(section.content) else section.content
            self.assertTrue(100 < len(rendered) < 3000)

            os.environ["HERMES_KANBAN_TASK"] = "t_worker123"
            worker_rendered = section.content({}) if callable(section.content) else section.content
            self.assertEqual(worker_rendered, "")
            os.environ.pop("HERMES_KANBAN_TASK", None)

            # 4. CLI subcommand, dispatch tick hook, and retro blueprint registered
            self.assertIn("factonio", mgr._cli_commands)
            self.assertTrue(any(cb for cb in mgr._hooks.get("on_kanban_dispatch_tick", [])))
            self.assertIn("factonio:retro", mgr._automation_blueprints)


if __name__ == "__main__":
    unittest.main()
