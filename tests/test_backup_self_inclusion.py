"""A backup must not sweep up its own previous backups.

Dockup lives inside the stacks folder, and its backup tars that whole
folder, so without care every archive holds every archive before it and
the file doubles nightly.
"""

from __future__ import annotations

import sys
import tarfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import backup  # noqa: E402


class SelfInclusion(unittest.TestCase):
    def test_the_data_folder_is_left_out(self):
        with TemporaryDirectory() as tmp:
            stacks = Path(tmp) / "stacks"
            (stacks / "jellyfin").mkdir(parents=True)
            (stacks / "jellyfin" / "compose.yaml").write_text("services: {}\n")
            data = stacks / "dockup" / "data"
            (data / "backups").mkdir(parents=True)
            (data / "backups" / "dockup-backup-old.tar.gz").write_bytes(b"x" * 4096)
            (stacks / "dockup" / "compose.yaml").write_text("services: {}\n")

            out = Path(tmp) / "out.tar.gz"
            with tarfile.open(out, "w:gz") as tar:
                backup._add_tree(tar, stacks, "stacks", skip={data})
            with tarfile.open(out) as tar:
                names = tar.getnames()

        self.assertTrue(any(n.endswith("jellyfin/compose.yaml") for n in names),
                        "another stack's compose file still goes in")
        self.assertTrue(any(n.endswith("dockup/compose.yaml") for n in names),
                        "Dockup's own compose file still goes in")
        self.assertFalse([n for n in names if "/data/" in n or n.endswith("/data")],
                         f"Dockup's data folder must be left out: {names}")


class WholeBackup(unittest.TestCase):
    """The real thing, not just the walker: make_backup must leave its
    own folder out."""

    def test_make_backup_leaves_its_own_output_behind(self):
        from app import config, db
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            stacks = root / "stacks"
            data = stacks / "dockup" / "data"
            (data / "backups").mkdir(parents=True)
            (stacks / "jellyfin").mkdir(parents=True)
            (stacks / "jellyfin" / "compose.yaml").write_text("services: {}\n")
            # A big earlier backup: the thing that must not be swept up.
            (data / "backups" / "dockup-backup-20261018-030000.tar.gz").write_bytes(b"x" * 200000)

            old = (config.DATA_DIR, config.BACKUP_DIR, config.STACKS_DIR, config.DB_PATH)
            config.DATA_DIR, config.BACKUP_DIR = data, data / "backups"
            config.STACKS_DIR, config.DB_PATH = stacks, data / "dockup.db"
            try:
                db.init()
                made, _ = backup.make_backup("test")
                with tarfile.open(made) as tar:
                    names = tar.getnames()
            finally:
                config.DATA_DIR, config.BACKUP_DIR, config.STACKS_DIR, config.DB_PATH = old

        self.assertIn("dockup.db", names, "the database still goes in, as a snapshot")
        self.assertTrue(any(n.endswith("jellyfin/compose.yaml") for n in names))
        self.assertFalse([n for n in names if "backups/dockup-backup-" in n],
                         f"an earlier backup must never be inside a backup: {names}")


class SizeAlarm(unittest.TestCase):
    def _run(self, last_size, new_size):
        with TemporaryDirectory() as tmp:
            backup.config.BACKUP_DIR = Path(tmp)
            (Path(tmp) / "dockup-backup-20261018-030000.tar.gz").write_bytes(b"x" * last_size)
            partial = Path(tmp) / ".partial-now.tar.gz"
            partial.write_bytes(b"x" * new_size)
            backup._check_size(partial)

    def test_a_runaway_backup_is_refused(self):
        with self.assertRaises(RuntimeError) as caught:
            self._run(100 * 1024 * 1024, 900 * 1024 * 1024)
        self.assertIn("has not been kept", str(caught.exception))

    def test_doubling_is_caught(self):
        # Doubling is exactly how the folder filled, so it must be caught.
        with self.assertRaises(RuntimeError):
            self._run(100 * 1024 * 1024, 230 * 1024 * 1024)

    def test_ordinary_growth_is_fine(self):
        self._run(100 * 1024 * 1024, 120 * 1024 * 1024)

    def test_a_small_backup_growing_is_not_an_alarm(self):
        # A new install goes from nearly nothing to a few megabytes.
        self._run(1024, 5 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
