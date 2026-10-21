"""Tests for the second round of hardening: encrypted two-factor secrets,
settings and request-body checks, shared folders on delete, restore file
names, and volumes removed only from the list that was shown."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_regressions import AppCase  # noqa: E402

from app import config, crypto, db, stacks  # noqa: E402


class TwoFactorSecretIsEncrypted(AppCase):
    def test_a_plain_secret_from_an_older_install_is_encrypted_at_start(self):
        con = db.connect()
        with con:
            con.execute("UPDATE users SET totp_secret='JBSWY3DPEHPK3PXP', totp_enabled=1 WHERE id=1")
        db.init()
        stored = db.connect().execute("SELECT totp_secret FROM users WHERE id=1").fetchone()[0]
        self.assertNotIn("JBSWY3DPEHPK3PXP", stored)
        self.assertEqual(crypto.decrypt(stored), "JBSWY3DPEHPK3PXP")
        db.init()  # a second start leaves it alone
        again = db.connect().execute("SELECT totp_secret FROM users WHERE id=1").fetchone()[0]
        self.assertEqual(stored, again)

    def test_enabling_stores_it_encrypted(self):
        import pyotp
        secret = self.client.post("/api/2fa/begin", headers=self.headers).get_json()["secret"]
        resp = self.post("/api/2fa/enable", {"code": pyotp.TOTP(secret).now()})
        self.assertEqual(resp.status_code, 200)
        stored = db.connect().execute("SELECT totp_secret FROM users WHERE id=1").fetchone()[0]
        self.assertNotEqual(stored, secret)
        self.assertEqual(crypto.decrypt(stored), secret)


class RequestBodiesAndSettings(AppCase):
    def test_a_list_body_is_a_400_not_a_500(self):
        resp = self.client.post("/api/2fa/disable", json=[1, 2], headers=self.headers)
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post("/api/settings", json=["x"], headers=self.headers)
        self.assertIn(resp.status_code, (200, 400))

    def test_other_settings_are_checked_when_saved(self):
        for body in ({"smtp.port": "mail"}, {"smtp.port": "70000"},
                     {"runtime.socket": "docker.sock"},
                     {"adopt.exclude_paths": "/ok, relative/path"}):
            resp = self.post("/api/settings", body)
            self.assertEqual(resp.status_code, 400, body)
        self.assertEqual(self.post("/api/settings", {"smtp.port": "587",
                                                     "runtime.socket": "/var/run/docker.sock",
                                                     "adopt.exclude_paths": "/a,/b"}).status_code, 200)

    def test_patch_needs_the_csrf_token(self):
        resp = self.client.patch("/api/settings", json={})
        self.assertIn(resp.status_code, (403, 405))
        self.assertNotEqual(resp.status_code, 200)


class SharedFoldersAreLeftAlone(AppCase):
    def test_a_folder_another_stack_also_mounts_is_not_offered_for_deletion(self):
        shared = self.stacks.parent / "mnt" / "photos"
        own = self.stacks.parent / "mnt" / "app-only"
        for folder in (shared, own):
            folder.mkdir(parents=True)
        (self.stacks / "jellyfin" / "compose.yaml").write_text(
            f"services:\n  a:\n    image: x\n    volumes:\n      - {shared}:/photos\n"
            f"      - {own}:/data\n")
        (self.stacks / "plex").mkdir()
        (self.stacks / "plex" / "compose.yaml").write_text(
            f"services:\n  b:\n    image: y\n    volumes:\n      - {shared}:/media\n")
        saved, stacks._SYSTEM_PATHS = stacks._SYSTEM_PATHS, ()  # the temp folder sits under /tmp
        self.addCleanup(setattr, stacks, "_SYSTEM_PATHS", saved)
        offered = [m["source"] for m in stacks._stack_data_paths("jellyfin")]
        self.assertIn(str(own), offered)
        self.assertNotIn(str(shared), offered)


class RestoreFileNames(AppCase):
    def test_only_real_backup_names_are_accepted(self):
        from app import backup
        for name in ("../../etc/passwd", "notes.txt", "dockup-backup-x.tar", ".partial-1.tar.gz/.."):
            with self.assertRaises(ValueError):
                backup.restore_backup(name)


class VolumesOnlyFromTheShownList(AppCase):
    def test_removing_volumes_without_the_shown_list_is_refused(self):
        resp = self.post("/api/system/prune", {"targets": ["volumes"]})
        self.assertEqual(resp.status_code, 400)

    def test_only_shown_volumes_that_are_still_unused_are_removed(self):
        from app import runtime
        removed = []

        class Engine:
            def dangling_volumes(self):
                return [{"name": "old_data", "size": ""}, {"name": "later_unused", "size": ""}]

            def remove_volume(self, name):
                removed.append(name)

        runtime.current = lambda: Engine()
        resp = self.post("/api/system/prune", {"targets": ["volumes"],
                                               "volumes": ["old_data", "now_in_use"]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(removed, ["old_data"])


def _make_stack_backup(folder, filename, compose, manifest="{}", pieces=("data-0.tar.gz",)):
    import io, tarfile
    path = folder / filename
    with tarfile.open(path, "w:gz") as tar:
        for name, text in (("compose.yaml", compose), ("manifest.json", manifest)):
            data = text.encode()
            info = tarfile.TarInfo(f"./{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for piece in pieces:
            info = tarfile.TarInfo(f"./{piece}")
            info.size = 0
            tar.addfile(info, io.BytesIO(b""))
    return path


class PerStackRestore(AppCase):
    def setUp(self):
        super().setUp()
        from app import runtime
        self.calls = []
        calls = self.calls

        class Engine:
            running = []

            def ps(self_inner):
                return Engine.running

            def restore_path_from_backup(self_inner, dest, piece):
                calls.append(("bind", dest))

            def restore_volume_from_backup(self_inner, vol, piece):
                calls.append(("volume", vol))

        self.Engine = Engine
        runtime.current = lambda: Engine()
        self.folder = config.STACK_BACKUP_DIR
        saved, stacks._SYSTEM_PATHS = stacks._SYSTEM_PATHS, ()
        self.addCleanup(setattr, stacks, "_SYSTEM_PATHS", saved)

    def restore(self, filename, force=False):
        return self.post(f"/api/stacks/jellyfin/backups/{filename}/restore", {"force": force})

    def test_a_tampered_backup_cannot_aim_at_a_system_folder(self):
        saved, stacks._SYSTEM_PATHS = stacks._SYSTEM_PATHS, ("/etc",)
        self.addCleanup(setattr, stacks, "_SYSTEM_PATHS", saved)
        _make_stack_backup(self.folder, "jellyfin-20260101-000001.tar.gz",
                           "services:\n  a:\n    image: x\n    volumes:\n      - /etc/ssh:/data\n")
        resp = self.restore("jellyfin-20260101-000001.tar.gz", force=True)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.calls, [])

    def test_it_will_not_write_into_another_stack_or_the_manifest_path(self):
        manifest = '{"mounts": [{"ok": true, "type": "bind", "source": "/etc", "archive": "data-0.tar.gz"}]}'
        other = self.stacks / "jellyfin" / "data"
        _make_stack_backup(self.folder, "jellyfin-20260101-000002.tar.gz",
                           f"services:\n  a:\n    image: x\n    volumes:\n      - {self.stacks}/dockup:/d\n",
                           manifest)
        resp = self.restore("jellyfin-20260101-000002.tar.gz", force=True)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.calls, [])

    def test_the_folders_are_shown_first_and_only_written_after_confirming(self):
        _make_stack_backup(self.folder, "jellyfin-20260101-000003.tar.gz",
                           "services:\n  a:\n    image: x\n    volumes:\n      - ./data:/d\n")
        first = self.restore("jellyfin-20260101-000003.tar.gz")
        self.assertEqual(first.status_code, 409)
        self.assertTrue(first.get_json()["confirm"])
        self.assertIn("jellyfin/data", first.get_json()["error"])
        self.assertEqual(self.calls, [])
        again = self.restore("jellyfin-20260101-000003.tar.gz", force=True)
        self.assertEqual(again.status_code, 200, again.get_json())
        self.assertEqual([c[0] for c in self.calls], ["bind"])
        self.assertTrue(self.calls[0][1].endswith("jellyfin/data"))

    def test_the_old_config_is_kept_and_a_running_stack_is_refused(self):
        (self.stacks / "jellyfin" / "compose.yaml").write_text("services: {old: {image: o}}\n")
        _make_stack_backup(self.folder, "jellyfin-20260101-000004.tar.gz", "services: {}\n", pieces=())
        self.Engine.running = [{"id": "abc", "project": "jellyfin", "state": "running", "name": "jellyfin-a-1"}]
        self.assertEqual(self.restore("jellyfin-20260101-000004.tar.gz", force=True).status_code, 400)
        self.assertIn("old", (self.stacks / "jellyfin" / "compose.yaml").read_text())
        self.Engine.running = []
        self.assertEqual(self.restore("jellyfin-20260101-000004.tar.gz", force=True).status_code, 200)
        self.assertIn("old", (self.stacks / "jellyfin" / "compose.yaml.before-restore").read_text())

    def test_a_backup_of_another_stack_is_refused_and_a_busy_stack_says_so(self):
        _make_stack_backup(self.folder, "plex-20260101-000001.tar.gz", "services: {}\n", pieces=())
        self.assertEqual(self.restore("plex-20260101-000001.tar.gz", force=True).status_code, 400)
        from app import locks
        _make_stack_backup(self.folder, "jellyfin-20260101-000005.tar.gz", "services: {}\n", pieces=())
        locks.try_acquire(locks.stack("jellyfin"), "An update")
        self.assertEqual(self.restore("jellyfin-20260101-000005.tar.gz", force=True).status_code, 409)

    def test_a_longer_stack_name_cannot_claim_another_stacks_backup(self):
        (self.stacks / "jelly").mkdir()
        (self.stacks / "jelly" / "compose.yaml").write_text("services: {}\n")
        _make_stack_backup(self.folder, "jelly-fin-20260101-000000.tar.gz", "services: {}\n", pieces=())
        resp = self.post("/api/stacks/jelly/backups/jelly-fin-20260101-000000.tar.gz/restore", {"force": True})
        self.assertEqual(resp.status_code, 400)

    def test_extra_reach_in_the_restored_compose_file_is_listed(self):
        _make_stack_backup(self.folder, "jellyfin-20260101-000001.tar.gz",
                           "services:\n  a:\n    image: x\n    privileged: true\n    pid: host\n", pieces=())
        first = self.restore("jellyfin-20260101-000001.tar.gz")
        self.assertEqual(first.status_code, 409)
        self.assertIn("privileged", first.get_json()["error"])
        self.assertIn("pid host", first.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
