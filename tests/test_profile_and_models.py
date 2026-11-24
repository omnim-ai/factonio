"""Clean-room tests for dedicated `factonio` profile setup and phase model resolution."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

from tests._env import PLUGIN_ROOT, isolated_hermes_home

if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))


class TestProfileAndModels(unittest.TestCase):
    def test_init_profile_idempotent_and_doctor_passes_profile_checks(self):
        import hermes_yaml as yaml
        from factonio.profile_setup import init_factonio_profile, run_doctor

        with isolated_hermes_home() as home:
            (home / "config.yaml").write_text(
                "model:\n  provider: openai-codex\n  default: gpt-5.3-codex\n",
                encoding="utf-8",
            )

            first = init_factonio_profile(PLUGIN_ROOT, apply_root_config=True, max_concurrent_workers=4)
            self.assertTrue(first["ok"])
            self.assertTrue(first["created"])
            self.assertTrue(first["soul_written"])
            self.assertEqual(first["plugin_link"]["action"], "linked")

            # Verify worker config
            wcfg = yaml.safe_load((home / "profiles" / "factonio" / "config.yaml").read_text())
            self.assertEqual(wcfg["approvals"]["mode"], "off")
            self.assertIn("factonio", wcfg["plugins"]["enabled"])
            self.assertEqual(wcfg["kanban"]["max_in_progress_per_profile"], 4)

            # Verify root config updates applied
            rcfg = yaml.safe_load((home / "config.yaml").read_text())
            self.assertIs(rcfg["kanban"]["dispatch_in_gateway"], True)
            self.assertIs(rcfg["kanban"]["auto_decompose"], False)
            self.assertEqual(rcfg["kanban"]["max_in_progress"], 4)

            # Second call is idempotent
            second = init_factonio_profile(PLUGIN_ROOT, apply_root_config=True)
            self.assertFalse(second["created"])
            self.assertFalse(second["soul_written"])
            self.assertEqual(second["plugin_link"]["action"], "unchanged")

            # Doctor checks on worker profile all pass
            doc = run_doctor(PLUGIN_ROOT)
            by_name = {c["name"]: c for c in doc["checks"]}
            for key in ("worker_profile", "worker_plugin_link", "worker_plugin_enabled", "worker_approvals_off"):
                self.assertTrue(by_name[key]["ok"], f"Check {key} failed: {by_name[key]}")

    def test_phase_model_resolution_precedence_and_cross_model_warning(self):
        from factonio.config import check_cross_model_warning, resolve_phase_model, save_settings
        from factonio.profile_setup import init_factonio_profile

        with isolated_hermes_home() as home:
            # Seed root profile with default model and distinct auxiliary task slots
            (home / "config.yaml").write_text(
                """model:
  provider: openai
  default: model-default
auxiliary:
  factonio_define:
    provider: anthropic
    model: claude-planner
  factonio_implement:
    provider: openai
    model: codex-builder
  factonio_verify:
    provider: openai
    model: codex-builder
""",
                encoding="utf-8",
            )
            init_factonio_profile(PLUGIN_ROOT)

            # Without auxiliary override for watchdog, falls back to profile default
            self.assertEqual(resolve_phase_model("define")["model"], "claude-planner")
            self.assertEqual(resolve_phase_model("implement")["model"], "codex-builder")
            self.assertEqual(resolve_phase_model("watchdog")["model"], "model-default")

            # Warning fires when verify == implement
            warn = check_cross_model_warning()
            self.assertIsNotNone(warn)
            self.assertIn("same model", warn or "")

            # Per-repo override wins over auxiliary slot and clears cross-model warning
            save_settings({
                "repos": {
                    "acme-api": {
                        "slug": "acme-api",
                        "repo": "acme/api",
                        "path": str(home / "acme-api"),
                        "models": {
                            "verify": {"provider": "anthropic", "model": "claude-verifier"},
                            "verify_blackbox": {"provider": "google", "model": "gemini-tester"},
                        },
                    }
                }
            })
            v_diff = resolve_phase_model("verify", repo_slug="acme-api", subphase="verify_diff")
            v_bb = resolve_phase_model("verify", repo_slug="acme-api", subphase="verify_blackbox")
            self.assertEqual((v_diff["provider"], v_diff["model"]), ("anthropic", "claude-verifier"))
            self.assertEqual((v_bb["provider"], v_bb["model"]), ("google", "gemini-tester"))
            self.assertIsNone(check_cross_model_warning(repo_slug="acme-api"))


if __name__ == "__main__":
    unittest.main()
