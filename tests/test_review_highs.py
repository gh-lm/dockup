"""Regression tests for the six High findings of the independent review.

Each one was reproduced against the code as it stood before it was fixed,
by running it rather than reading it. They go through the real HTTP
endpoints where the fault was in an endpoint, and use the production
folder layout (Dockup's data folder reached by two paths) where that
mattered.
"""

from __future__ import annotations

import io
import json
import sys
import tarfile
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import backup, config, db, locks, predump, runtime  # noqa: E402

_KEYS = ("DATA_DIR", "BACKUP_DIR", "STACKS_DIR", "STACK_BACKUP_DIR", "DB_PATH",
         "MOCK_MODE", "SECRET_KEY")


class FakeEngine:
    """Answers ps() with whatever containers a test wants running."""

    def __init__(self, containers=()):
        self.containers = list(containers)

    def ps(self):
        return self.containers


class AppCase(unittest.TestCase):
    """A real app on a throwaway folder layout, signed in."""

    def setUp(self):
        self.tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tmp.name)
        self.saved = {k: getattr(config, k) for k in _KEYS}
        self.real_data = root / "stacks" / "dockup" / "data"
        (self.real_data / "backups").mkdir(parents=True)
        (self.real_data / "stack-backups").mkdir(parents=True)
        self.alias = root / "app-data"
        self.alias.symlink_to(self.real_data)
        self.stacks = root / "stacks"
        for name in ("jellyfin", "dockup"):
            (self.stacks / name).mkdir(exist_ok=True)
            (self.stacks / name / "compose.yaml").write_text("services: {}\n")
        config.DATA_DIR = self.alias
        config.BACKUP_DIR = self.alias / "backups"
        config.STACK_BACKUP_DIR = self.alias / "stack-backups"
        config.STACKS_DIR = self.stacks
        config.DB_PATH = self.alias / "dockup.db"
        config.MOCK_MODE = True
        config.SECRET_KEY = "k" * 40

        from app import create_app, updatecheck
        self._patched = (backup.start_scheduler, predump.start_scheduler, updatecheck.start)
        backup.start_scheduler = lambda: None
        predump.start_scheduler = lambda: None
        updatecheck.start = lambda app: None
        self.app = create_app()
        self.client = self.app.test_client()
        con = db.connect()
        with con:
            con.execute("INSERT OR IGNORE INTO users(id, username, pw_hash) VALUES(1, 'admin', 'x')")
        con.close()
        with self.client.session_transaction() as sess:
            sess["uid"] = 1
            sess["csrf"] = "token"
        self.headers = {"X-CSRF": "token"}
        self.old_current = runtime.current

    def tearDown(self):
        from app import updatecheck
        backup.start_scheduler, predump.start_scheduler, updatecheck.start = self._patched
        runtime.current = self.old_current
        for k, v in self.saved.items():
            setattr(config, k, v)
        for key in list(locks._held):
            locks.release(key)
        self.tmp.cleanup()

    def post(self, path, body=None):
        return self.client.post(path, json=body or {}, headers=self.headers)


class Finding1_DeletingDockupItself(AppCase):
    def test_stop_down_and_delete_are_refused_on_its_own_stack(self):
        for action in ("delete", "down", "stop"):
            resp = self.post(f"/api/stacks/dockup/action/{action}")
            self.assertEqual(resp.status_code, 400, action)
            self.assertIn("Dockup itself", resp.get_json()["error"])
        self.assertTrue((self.stacks / "dockup" / "compose.yaml").exists(),
                        "nothing may have been removed")

    def test_the_engine_not_answering_fails_closed(self):
        class Down:
            def ps(self):
                raise runtime.RuntimeError_("socket gone")
        runtime.current = lambda: Down()
        resp = self.post("/api/stacks/jellyfin/action/restart")
        self.assertEqual(resp.status_code, 502)
        self.assertIn("nothing was done", resp.get_json()["error"])


class Finding2_NothingStopsTwoThingsAtOnce(AppCase):
    def test_a_second_action_on_the_same_stack_is_refused(self):
        self.assertTrue(locks.try_acquire(locks.stack("jellyfin"), "Restart on jellyfin"))
        resp = self.post("/api/stacks/jellyfin/action/restart")
        body = resp.get_data(as_text=True)
        self.assertIn("already running", body)
        self.assertIn("[dockup-done:error]", body)

    def test_a_backup_cannot_start_while_another_maintenance_job_holds_the_lock(self):
        self.assertTrue(locks.try_acquire(locks.MAINTENANCE, "A restore"))
        with self.assertRaises(locks.Busy):
            backup.make_backup("manual")
        resp = self.post("/api/backup/run")
        self.assertEqual(resp.status_code, 409)

    def test_two_backups_never_share_a_partial_name(self):
        first, _ = backup.make_backup("manual")
        second, _ = backup.make_backup("manual")
        self.assertNotEqual(first, second)
        self.assertEqual(sorted(p.name for p in config.BACKUP_DIR.glob(".partial-*")), [])

    def test_the_lock_is_released_after_an_action(self):
        self.post("/api/stacks/jellyfin/action/restart")
        self.assertFalse(locks.what_is_running(locks.stack("jellyfin")))


class Finding3_Export(AppCase):
    def test_export_streams_from_disk_and_leaves_the_data_folder_out(self):
        (self.real_data / "backups" / "dockup-backup-20261018-030000.tar.gz").write_bytes(b"x" * 50000)
        resp = self.client.get("/api/backup/export", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.data
        resp.close()
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = z.namelist()
        self.assertIn("dockup.db", names)
        self.assertTrue(any(n.endswith("jellyfin/compose.yaml") for n in names))
        self.assertFalse([n for n in names if "dockup-backup-20261018" in n],
                         "an export must not contain the backups folder")
        leftovers = [p.name for p in self.real_data.glob(".export-*")]
        self.assertEqual(leftovers, [], "the temporary file is removed once it is sent")
        self.assertFalse(locks.what_is_running(locks.MAINTENANCE), "and the lock is released")


class Finding4_Restore(AppCase):
    def _backup(self):
        made, _ = backup.make_backup("manual")
        return made

    def test_refused_while_a_stack_is_running(self):
        made = self._backup()
        runtime.current = lambda: FakeEngine([
            {"id": "aaaaaaaaaaaa", "name": "j", "project": "jellyfin", "state": "running"}])
        with self.assertRaises(ValueError) as caught:
            backup.restore_backup(made.name)
        self.assertIn("jellyfin", str(caught.exception))
        self.assertIn("Stop these stacks", str(caught.exception))

    def test_refused_when_the_engine_cannot_be_asked(self):
        made = self._backup()

        class Down:
            def ps(self):
                raise runtime.RuntimeError_("socket gone")
        runtime.current = lambda: Down()
        with self.assertRaises(ValueError) as caught:
            backup.restore_backup(made.name)
        self.assertIn("Nothing has been changed", str(caught.exception))

    def test_a_backup_that_left_files_out_needs_a_deliberate_yes(self):
        made = self._backup()
        # add the note a backup writes when it could not read something
        extra = made.parent / "dockup-backup-with-gaps.tar.gz"
        with tarfile.open(made) as src, tarfile.open(extra, "w:gz") as dst:
            for member in src.getmembers():
                dst.addfile(member, src.extractfile(member) if member.isfile() else None)
            note = b"/opt/stacks/jellyfin/db/pg_wal (Permission denied)\n"
            info = tarfile.TarInfo(backup.SKIPPED_NOTE)
            info.size = len(note)
            dst.addfile(info, io.BytesIO(note))
        runtime.current = lambda: FakeEngine([])
        with self.assertRaises(backup.NeedsConfirmation):
            backup.restore_backup(extra.name)
        # nothing was touched by the refusal
        self.assertTrue((self.stacks / "jellyfin" / "compose.yaml").exists())
        backup.restore_backup(extra.name, force=True)

    def test_restore_swaps_each_stack_and_keeps_the_old_ones(self):
        made = self._backup()
        (self.stacks / "jellyfin" / "compose.yaml").write_text("services: {changed: {}}\n")
        (self.stacks / "extra").mkdir()
        (self.stacks / "extra" / "compose.yaml").write_text("services: {}\n")
        runtime.current = lambda: FakeEngine([])
        backup.restore_backup(made.name)
        self.assertEqual((self.stacks / "jellyfin" / "compose.yaml").read_text(), "services: {}\n")
        self.assertFalse((self.stacks / "extra").exists(), "a stack made after the backup is moved aside")
        kept = self.real_data / "pre-restore-stacks"
        self.assertEqual((kept / "extra" / "compose.yaml").read_text(), "services: {}\n")
        self.assertEqual((kept / "jellyfin" / "compose.yaml").read_text(),
                         "services: {changed: {}}\n")
        self.assertTrue((self.stacks / "dockup" / "compose.yaml").exists())


class Finding5_RetentionAndSettings(AppCase):
    def test_zero_days_cannot_delete_the_newest_backup(self):
        made, _ = backup.make_backup("manual")
        backup.apply_retention(0)
        backup.apply_retention(-5)
        self.assertTrue(made.exists(), "the backup just made must survive any retention")

    def test_old_backups_still_go_and_the_newest_stays(self):
        import os
        import time
        old = config.BACKUP_DIR / "dockup-backup-20200101-030000.tar.gz"
        old.write_bytes(b"x")
        os.utime(old, (time.time() - 90 * 86400,) * 2)
        made, _ = backup.make_backup("manual")
        backup.apply_retention(14)
        self.assertFalse(old.exists())
        self.assertTrue(made.exists())

    def test_nonsense_values_are_refused_when_saved(self):
        for key, bad in (("backup.retention_days", "0"), ("backup.retention_days", "-3"),
                         ("backup.retention_days", "lots"), ("backup.hour", "24"),
                         ("backup.hour", "-1"), ("backup.hour", "soon"), ("predump.hour", "99")):
            resp = self.post("/api/settings", {key: bad})
            self.assertEqual(resp.status_code, 400, f"{key}={bad}")
        for key, good in (("backup.retention_days", "7"), ("backup.hour", "3"), ("predump.hour", "2")):
            resp = self.post("/api/settings", {key: good})
            self.assertEqual(resp.status_code, 200, f"{key}={good}")

    def test_a_bad_stored_value_cannot_break_the_run_button(self):
        con = db.connect()
        with con:
            con.execute("INSERT INTO settings(key, value) VALUES('backup.retention_days', 'lots') "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
        con.close()
        self.assertEqual(self.post("/api/backup/run").status_code, 200)


class DumpQuietFailures(AppCase):
    GOOD = {"name": "f", "container": "forgejo", "command": "forgejo dump", "file": "f.zip"}

    def test_a_quoting_mistake_is_caught_when_saved(self):
        bad = dict(self.GOOD, command='forgejo dump "unclosed')
        with self.assertRaises(ValueError) as caught:
            predump.validate_jobs(json.dumps([bad]))
        self.assertIn("quoting", str(caught.exception))

    def test_an_invalid_saved_list_is_reported_not_called_healthy(self):
        con = db.connect()
        with con:
            con.execute("INSERT INTO settings(key, value) VALUES('predump.jobs', 'nonsense') "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
        con.close()
        status = predump.run_all("test")
        self.assertFalse(status["ok"])
        self.assertIn("config_error", status)
        written = json.loads((predump.folder() / predump.STATUS_NAME).read_text())
        self.assertFalse(written["ok"])

    def test_a_scheduled_job_is_held_to_the_time_left(self):
        self.assertLessEqual(predump._seconds_to_deadline(), 24 * 3600)
        self.assertEqual(predump.run_hour(), predump.DEFAULT_RUN_HOUR)


if __name__ == "__main__":
    unittest.main()
