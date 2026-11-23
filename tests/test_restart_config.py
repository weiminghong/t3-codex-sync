"""Check executable discovery without interacting with a running T3 instance."""

import contextlib
import io
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import t3_codex_sync as sync


class RestartConfigTests(unittest.TestCase):
    def test_explicit_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            exe = Path(directory) / "T3 Code (Beta).exe"
            exe.touch()
            self.assertEqual(sync.resolve_t3_executable(exe), exe.resolve())

    def test_executable_on_path(self):
        with tempfile.TemporaryDirectory() as directory:
            exe = Path(directory) / "T3 Code (Beta).exe"
            exe.touch()
            with patch.object(sync.shutil, "which", return_value=str(exe)):
                self.assertEqual(sync.resolve_t3_executable(exe.name), exe.resolve())

    def test_missing_executable_leaves_t3_and_database_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "test.sqlite"
            sqlite3.connect(db).close()
            before = db.read_bytes()
            args = sync.build_parser().parse_args(["sync", "--db", str(db), "--restart"])
            with patch.object(sync, "plan_sync", return_value=[SimpleNamespace(kind="merge")]), \
                    patch.object(sync, "format_actions_report", return_value="synthetic preview"), \
                    patch.object(sync, "is_t3_running", return_value=[123]), \
                    patch.object(sync, "resolve_t3_executable", return_value=None), \
                    patch.object(sync, "stop_t3_gracefully") as stop, \
                    patch.object(sync, "execute_sync") as execute, \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(sync.cmd_status_or_sync(args, dry_run=False), 2)
                stop.assert_not_called()
                execute.assert_not_called()
            self.assertEqual(db.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
