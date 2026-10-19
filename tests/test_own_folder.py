"""Dockup lives inside the stacks folder, so backup and restore both have
to leave its own folder alone.

In the container the data folder is one directory seen at two paths: its
own mount at /app/data and again inside the stacks folder. These tests
reproduce that with a symlink, because a test that puts DATA_DIR at the
same string as the path inside the stacks folder passes even when the
code compares paths as text.
"""

from __future__ import annotations

import sys
import tarfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import backup, config, db, runtime  # noqa: E402


class Layout:
    """/stacks/dockup/data is the real folder; /app-data points at it."""

    def __init__(self):
        self.tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tmp.name)
        self.stacks = root / "stacks"
        self.real_data = self.stacks / "dockup" / "data"
        (self.real_data / "backups").mkdir(parents=True)
        (self.stacks / "jellyfin").mkdir(parents=True)
        (self.stacks / "jellyfin" / "compose.yaml").write_text("services: {}\n")
        (self.stacks / "dockup" / "compose.yaml").write_text("services: {}\n")
        (self.stacks / "dockup" / ".env").write_text("SECRET_KEY=not-a-real-key\n")
        self.alias = root / "app-data"
        self.alias.symlink_to(self.real_data)
        self.saved = (config.DATA_DIR, config.BACKUP_DIR, config.STACKS_DIR, config.DB_PATH)
        config.DATA_DIR = self.alias
        config.BACKUP_DIR = self.alias / "backups"
        config.STACKS_DIR = self.stacks
        config.DB_PATH = self.alias / "dockup.db"
        db.init()

    def close(self):
        config.DATA_DIR, config.BACKUP_DIR, config.STACKS_DIR, config.DB_PATH = self.saved
        self.tmp.cleanup()


class BackupLeavesItsOwnFolderOut(unittest.TestCase):
    def setUp(self):
        self.layout = Layout()

    def tearDown(self):
        self.layout.close()

    def test_an_earlier_backup_is_never_inside_a_new_one(self):
        (self.layout.real_data / "backups" / "dockup-backup-20261018-030000.tar.gz").write_bytes(
            b"x" * 200000)
        made, _ = backup.make_backup("manual")
        with tarfile.open(made) as tar:
            names = tar.getnames()
        self.assertFalse([n for n in names if "dockup-backup-20261018" in n],
                         "the data folder is reached by a second path and must still be skipped")
        self.assertIn("dockup.db", names, "the database still goes in as a snapshot")
        self.assertTrue(any(n.endswith("jellyfin/compose.yaml") for n in names))


class RestoreLeavesItsOwnFolderAlone(unittest.TestCase):
    def setUp(self):
        self.layout = Layout()

    def tearDown(self):
        self.layout.close()

    def test_restore_does_not_recurse_or_wipe_dockup(self):
        class NothingRunning:
            def ps(self):
                return []
        real_current = runtime.current
        runtime.current = lambda: NothingRunning()
        self.addCleanup(setattr, runtime, "current", real_current)
        made, _ = backup.make_backup("manual")
        # Change another stack after the backup, so a restore has work to do.
        (self.layout.stacks / "jellyfin" / "compose.yaml").write_text("services: {changed: {}}\n")

        backup.restore_backup(made.name)

        self.assertEqual((self.layout.stacks / "jellyfin" / "compose.yaml").read_text(),
                         "services: {}\n", "the other stack is put back")
        for kept in ("compose.yaml", ".env"):
            self.assertTrue((self.layout.stacks / "dockup" / kept).exists(),
                            f"Dockup's own {kept} must survive a restore")
        self.assertTrue((self.layout.real_data / "dockup.db").exists(),
                        "Dockup's database must survive a restore")
        self.assertTrue(made.exists(), "the backups must survive a restore")
        nested = list(self.layout.real_data.rglob("pre-restore-stacks/dockup"))
        self.assertEqual(nested, [], "the safety copy must not copy Dockup into itself")


class OwnFolderLookup(unittest.TestCase):
    def test_found_by_identity_and_absent_elsewhere(self):
        layout = Layout()
        try:
            self.assertEqual(backup.own_folder_in_stacks(), layout.stacks / "dockup")
            config.DATA_DIR = layout.stacks / "jellyfin"
            self.assertEqual(backup.own_folder_in_stacks(), layout.stacks / "jellyfin")
            config.DATA_DIR = Path(layout.tmp.name)
            self.assertIsNone(backup.own_folder_in_stacks())
        finally:
            layout.close()


if __name__ == "__main__":
    unittest.main()
