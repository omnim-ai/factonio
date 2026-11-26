"""Clean-room tests for dedicated `factonio` profile setup and phase model resolution."""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Iterator
from unittest import mock

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

    def test_worker_soul_execution_contract_preserves_information_barriers(self):
        from factonio.profile_setup import WORKER_SOUL_MD, init_factonio_profile

        with isolated_hermes_home() as home:
            init_factonio_profile(PLUGIN_ROOT)
            soul = (home / "profiles" / "factonio" / "SOUL.md").read_text(encoding="utf-8")
        self.assertEqual(soul, WORKER_SOUL_MD)

        # The original five rules stay intact and in order.
        for rule in (
            "1. Start every card by following the preloaded `factonio:*` skill instructions for your phase.",
            "2. Stay strictly within the scope and information barrier of your card:",
            "3. Verify claims with real commands and test output before completing a card.",
            "4. Keep comments and completion summaries concise and structured.",
            "5. Never invent secrets, never bypass CI gates,",
        ):
            self.assertIn(rule, soul)

        # Information barriers per phase are unchanged.
        self.assertIn("Design cards read `spec.md` and the repo; never look for or read `holdout/` scenarios.", soul)
        self.assertIn("Implementation cards read `spec.md`, approved `design/` artifacts (if linked), and code; never look for or read `holdout/` scenarios.", soul)
        self.assertIn("Diff Check cards read `spec.md`, approved `design/` artifacts (if linked), and the PR diff; never read `holdout/`.", soul)
        self.assertIn("never read `git diff` or implementation source files.", soul)

        # New execution contract adapted from pstack's worker.md.
        self.assertIn("## Execution Contract", soul)
        self.assertIn("acceptance bar is the whole bar", soul)
        self.assertIn("reversible implementation choices", soul)
        self.assertIn("record the rationale", soul)
        self.assertIn("product decisions, missing access, or irreversible risk", soul)
        self.assertIn("pre-existing base defects", soul)
        self.assertIn("newly reaches or copies", soul)
        self.assertLess(soul.index("5. Never invent secrets"), soul.index("## Execution Contract"))

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


def _neutral_temp_base() -> str:
    """A writable temp base outside every path the link guard treats as ephemeral."""
    from factonio.profile_setup import _is_ephemeral_path

    for cand in (tempfile.gettempdir(), "/tmp", "/var/tmp"):
        if os.path.isdir(cand) and os.access(cand, os.W_OK) and not _is_ephemeral_path(cand):
            return cand
    raise unittest.SkipTest("no writable temp directory outside ephemeral paths")


@contextlib.contextmanager
def _persistent_profile_home() -> Iterator[Path]:
    """isolated_hermes_home() that the guard sees as a persistent (non-temp, non-ephemeral) Hermes home."""
    with mock.patch.object(tempfile, "tempdir", _neutral_temp_base()):
        ctx = isolated_hermes_home()
        home = ctx.__enter__()
    try:
        elsewhere = str(home.parent / "not-the-system-temp-dir")
        env = {k: v for k, v in os.environ.items() if k != "HERMES_SCRATCH_DIR"}
        with mock.patch("factonio.profile_setup.tempfile.gettempdir", return_value=elsewhere), \
                mock.patch.dict(os.environ, env, clear=True):
            yield home
    finally:
        ctx.__exit__(None, None, None)


class TestWorkerPluginLinkGuard(unittest.TestCase):
    def _dirs(self, home: Path) -> tuple[Path, Path]:
        stable = home.parent / "src" / "factonio"
        ephemeral = home.parent / "src" / "factonio" / ".worktrees" / "t_abc123"
        ephemeral.mkdir(parents=True)
        return stable, ephemeral

    def test_ephemeral_path_and_isolated_profile_detection(self):
        from factonio.profile_setup import _is_ephemeral_path, _is_isolated_profile_dir

        with isolated_hermes_home() as home:
            self.assertTrue(_is_ephemeral_path(home / "kanban" / "workspaces" / "t_1"))
            self.assertTrue(_is_ephemeral_path("/srv/repo/.worktrees/t_1/sub"))
            self.assertTrue(_is_ephemeral_path("/srv/h/profiles/factonio/cache/scratch/x"))
            custom_ws, scratch = home.parent / "boards" / "ws", home.parent / "scr"
            with mock.patch.dict(os.environ, {
                "HERMES_KANBAN_WORKSPACES_ROOT": str(custom_ws), "HERMES_SCRATCH_DIR": str(scratch),
            }):
                self.assertTrue(_is_ephemeral_path(custom_ws / "t_9"))
                self.assertTrue(_is_ephemeral_path(scratch / "probe"))
            self.assertFalse(_is_ephemeral_path("/srv/dev/factonio"))
            self.assertFalse(_is_ephemeral_path("/srv/dev/my.worktrees-notes/cache"))
            self.assertTrue(_is_isolated_profile_dir(home / "profiles" / "factonio"))
            self.assertTrue(_is_isolated_profile_dir("/srv/repo/.worktrees/t_1/.hermes/profiles/factonio"))
        with _persistent_profile_home() as home:
            self.assertFalse(_is_isolated_profile_dir(home / "profiles" / "factonio"))

    def test_persistent_profile_refuses_ephemeral_target_without_force(self):
        from factonio.profile_setup import _ensure_plugin_link, init_factonio_profile

        with _persistent_profile_home() as home:
            stable, ephemeral = self._dirs(home)
            profile_dir = home / "profiles" / "factonio"
            link = profile_dir / "plugins" / "factonio"

            fresh = init_factonio_profile(ephemeral)
            self.assertTrue(fresh["created"])
            self.assertEqual(fresh["plugin_link"]["action"], "skipped_ephemeral_guard")
            self.assertTrue(fresh["plugin_link"]["guarded"])
            self.assertIsNone(fresh["plugin_link"]["target"])
            self.assertIn("--force-link", fresh["plugin_link"]["warning"])
            self.assertEqual(fresh["warnings"], [fresh["plugin_link"]["warning"]])
            self.assertFalse(link.exists() or link.is_symlink())
            self.assertEqual(_ensure_plugin_link(profile_dir, ephemeral)["action"], "skipped_ephemeral_guard")

            first = init_factonio_profile(stable)
            self.assertEqual(first["plugin_link"]["action"], "linked")
            self.assertEqual(first["warnings"], [])

            again = init_factonio_profile(ephemeral)
            self.assertEqual(again["plugin_link"]["action"], "preserved_ephemeral_guard")
            self.assertTrue(again["plugin_link"]["guarded"])
            self.assertEqual(again["plugin_link"]["requested_target"], str(ephemeral.resolve()))
            self.assertEqual(again["warnings"], [again["plugin_link"]["warning"]])
            self.assertEqual(link.resolve(), stable.resolve())

            forced = init_factonio_profile(ephemeral, force_plugin_link=True)
            self.assertEqual(forced["plugin_link"]["action"], "linked")
            self.assertNotIn("guarded", forced["plugin_link"])
            self.assertEqual(link.resolve(), ephemeral.resolve())

            back = init_factonio_profile(stable)
            self.assertEqual(back["plugin_link"]["action"], "linked")
            self.assertEqual(link.resolve(), stable.resolve())

    def test_isolated_temp_home_may_link_ephemeral_checkout(self):
        from factonio.profile_setup import init_factonio_profile

        with isolated_hermes_home() as home:
            _, ephemeral = self._dirs(home)
            res = init_factonio_profile(ephemeral)
            self.assertEqual(res["plugin_link"]["action"], "linked")
            self.assertEqual(res["warnings"], [])
            self.assertEqual((home / "profiles" / "factonio" / "plugins" / "factonio").resolve(), ephemeral.resolve())

    def test_doctor_warns_when_persistent_link_points_into_ephemeral_path(self):
        from factonio.profile_setup import init_factonio_profile, run_doctor

        with _persistent_profile_home() as home:
            stable, ephemeral = self._dirs(home)
            init_factonio_profile(stable)
            doc = run_doctor(stable)
            link_check = next(c for c in doc["checks"] if c["name"] == "worker_plugin_link")
            self.assertTrue(link_check["ok"])
            self.assertNotIn("ephemeral", link_check)
            self.assertFalse(any("ephemeral path" in w for w in doc["warnings"]))

            init_factonio_profile(ephemeral, force_plugin_link=True)
            doc = run_doctor(stable)
            link_check = next(c for c in doc["checks"] if c["name"] == "worker_plugin_link")
            self.assertTrue(link_check["ok"])
            self.assertIs(link_check["ephemeral"], True)
            hits = [w for w in doc["warnings"] if "ephemeral path" in w]
            self.assertEqual(len(hits), 1)
            self.assertIn(str(ephemeral.resolve()), hits[0])

        with isolated_hermes_home() as home:
            _, ephemeral = self._dirs(home)
            init_factonio_profile(ephemeral)
            doc = run_doctor(ephemeral)
            self.assertFalse(any("ephemeral path" in w for w in doc["warnings"]))

    def test_cli_init_force_link_flag(self):
        import argparse
        import io
        import json

        from factonio.commands import register_cli

        class _Ctx:
            def __init__(self) -> None:
                self.cli: dict = {}

            def register_cli_command(self, name, *, help, setup_fn, handler_fn):
                self.cli[name] = (setup_fn, handler_fn)

            def __getattr__(self, name):
                return lambda *a, **kw: None

        with _persistent_profile_home() as home:
            stable, ephemeral = self._dirs(home)
            ctx = _Ctx()
            register_cli(ctx, ephemeral)
            setup_fn, handler_fn = ctx.cli["factonio"]
            parser = argparse.ArgumentParser(prog="hermes factonio")
            setup_fn(parser)

            def run(*argv: str) -> dict:
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    handler_fn(parser.parse_args(list(argv)))
                return json.loads(out.getvalue())

            guarded = run("--json", "init")
            self.assertEqual(guarded["plugin_link"]["action"], "skipped_ephemeral_guard")
            forced = run("--json", "init", "--force-link")
            self.assertEqual(forced["plugin_link"]["action"], "linked")
            self.assertEqual(
                (home / "profiles" / "factonio" / "plugins" / "factonio").resolve(), ephemeral.resolve()
            )


if __name__ == "__main__":
    unittest.main()
