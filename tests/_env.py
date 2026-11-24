"""Shared clean-room HERMES_HOME fixture helper for Factonio tests.

Never touches the operator's real ~/.hermes or kanban.db.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path
from typing import Iterator

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


@contextlib.contextmanager
def isolated_hermes_home() -> Iterator[Path]:
    """Run inside a brand-new temporary HERMES_HOME with isolated Kanban env vars."""
    with tempfile.TemporaryDirectory(prefix="factonio-test-home-") as tmp:
        fake_user_home = Path(tmp)
        home = fake_user_home / ".hermes"
        home.mkdir(parents=True, exist_ok=True)
        kanban_db = home / "kanban.db"
        ws_root = home / "kanban" / "workspaces"
        ws_root.mkdir(parents=True, exist_ok=True)

        keys = [
            "HOME",
            "HERMES_HOME",
            "HERMES_PROFILE",
            "HERMES_KANBAN_DB",
            "HERMES_KANBAN_BOARD",
            "HERMES_KANBAN_WORKSPACES_ROOT",
            "HERMES_KANBAN_TASK",
            *(k for k in os.environ if k.startswith("HERMES_SESSION_")),
        ]
        saved = {k: os.environ.get(k) for k in keys}
        os.environ["HOME"] = str(fake_user_home)
        os.environ["HERMES_HOME"] = str(home)
        os.environ["HERMES_KANBAN_DB"] = str(kanban_db)
        os.environ["HERMES_KANBAN_BOARD"] = "default"
        os.environ["HERMES_KANBAN_WORKSPACES_ROOT"] = str(ws_root)
        os.environ.pop("HERMES_PROFILE", None)
        os.environ.pop("HERMES_KANBAN_TASK", None)
        for k in list(os.environ):
            if k.startswith("HERMES_SESSION_"):
                os.environ.pop(k, None)

        # Reset Hermes's cached HERMES_HOME override / kanban init cache if loaded.
        try:
            from hermes_cli import kanban_db_connect as kdc

            kdc._INITIALIZED_PATHS.clear()
        except Exception:
            pass
        try:
            from hermes_cli import projects_db as pdb

            pdb._INITIALIZED_PATHS.clear()
        except Exception:
            pass

        try:
            yield home
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
