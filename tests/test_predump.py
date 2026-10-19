"""Dumps written before the nightly off-machine copy.

The two rules that matter: a failed dump never replaces a good one, and
the status file is written last, so anything that reads it can trust
what it describes is already on disk.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db, predump, runtime  # noqa: E402


class FakeRuntime:
    def __init__(self, behaviour):
        self.behaviour = behaviour

    def exec_to_file(self, container, command, out, timeout=1200):
        what = self.behaviour.get(container, "ok")
        if what == "fail":
            raise runtime.RuntimeError_("the container said no")
        if what == "empty":
            return
        out.write(b"a consistent copy of " + container.encode())


class PreDump(unittest.TestCase):
    def setUp(self):
        # ignore_cleanup_errors: logging to the activity table leaves a
        # SQLite connection open, and its -wal and -shm files can appear
        # while the folder is being removed.
        self.tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tmp.name)
        self.old = (config.DATA_DIR, config.DB_PATH)
        self.old_runtime = runtime.current
        config.DATA_DIR = root
        config.DB_PATH = root / "dockup.db"
        db.init()

    def tearDown(self):
        config.DATA_DIR, config.DB_PATH = self.old
        runtime.current = self.old_runtime
        self.tmp.cleanup()

    def _set_jobs(self, jobs):
        con = db.connect()
        with con:
            con.execute(
                "INSERT INTO settings(key, value) VALUES('predump.jobs', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(jobs),),
            )
        con.close()

    def _status(self):
        return json.loads((predump.folder() / predump.STATUS_NAME).read_text())

    def test_a_dump_is_written_and_described(self):
        self._set_jobs([{"name": "forgejo", "container": "forgejo",
                         "command": ["forgejo", "dump", "-f", "-"], "file": "forgejo.zip"}])
        runtime.current = lambda: FakeRuntime({})
        predump.run_all("test")
        out = predump.folder() / "forgejo.zip"
        self.assertTrue(out.exists())
        st = self._status()
        self.assertTrue(st["ok"])
        entry = st["stacks"][0]
        self.assertEqual(entry["name"], "forgejo")
        self.assertEqual(entry["size"], out.stat().st_size)
        self.assertEqual(len(entry["sha256"]), 64)
        self.assertIn("written_at", st)

    def test_a_failed_dump_keeps_the_last_good_one(self):
        self._set_jobs([{"name": "forgejo", "container": "forgejo",
                         "command": ["forgejo", "dump"], "file": "forgejo.zip"}])
        runtime.current = lambda: FakeRuntime({})
        predump.run_all("first")
        good = (predump.folder() / "forgejo.zip").read_bytes()

        runtime.current = lambda: FakeRuntime({"forgejo": "fail"})
        predump.run_all("second")
        self.assertEqual((predump.folder() / "forgejo.zip").read_bytes(), good,
                         "the good dump must survive a failed run")
        st = self._status()
        self.assertFalse(st["ok"])
        entry = st["stacks"][0]
        self.assertFalse(entry["ok"])
        self.assertTrue(entry["kept_previous"])
        self.assertIn("old", entry["previous_age"])

    def test_a_command_that_writes_nothing_is_a_failure(self):
        self._set_jobs([{"name": "forgejo", "container": "forgejo",
                         "command": ["forgejo", "dump"], "file": "forgejo.zip"}])
        runtime.current = lambda: FakeRuntime({"forgejo": "empty"})
        predump.run_all("test")
        self.assertFalse((predump.folder() / "forgejo.zip").exists())
        self.assertFalse(self._status()["ok"])

    def test_nothing_configured_is_not_a_failure(self):
        runtime.current = lambda: FakeRuntime({})
        predump.run_all("test")
        st = self._status()
        self.assertTrue(st["ok"])
        self.assertEqual(st["stacks"], [])

    def test_a_half_written_dump_never_takes_the_real_name(self):
        self._set_jobs([{"name": "forgejo", "container": "forgejo",
                         "command": ["forgejo", "dump"], "file": "forgejo.zip"}])
        runtime.current = lambda: FakeRuntime({"forgejo": "fail"})
        predump.run_all("test")
        leftovers = [p.name for p in predump.folder().iterdir()
                     if p.name.startswith(".partial")]
        self.assertEqual(leftovers, [], "no half-written file is left behind")


class Validation(unittest.TestCase):
    GOOD = {"name": "f", "container": "forgejo", "command": ["forgejo", "dump"], "file": "f.zip"}

    def _check(self, **changes):
        job = dict(self.GOOD)
        job.update(changes)
        return predump.validate_jobs(json.dumps([job]))

    def test_a_good_list_passes(self):
        self.assertEqual(len(self._check()), 1)
        self.assertEqual(predump.validate_jobs(""), [])

    def test_a_file_name_cannot_climb_out_of_the_folder(self):
        for bad in ("../x.zip", "/etc/x", "a/b.zip", ".hidden", predump.STATUS_NAME):
            with self.assertRaises(ValueError, msg=bad):
                self._check(file=bad)

    def test_a_container_name_cannot_be_an_option(self):
        with self.assertRaises(ValueError):
            self._check(container="--privileged")

    def test_missing_fields_and_duplicates_are_refused_in_plain_words(self):
        with self.assertRaises(ValueError) as caught:
            predump.validate_jobs(json.dumps([{"container": "x"}]))
        self.assertIn("command", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            predump.validate_jobs(json.dumps([self.GOOD, self.GOOD]))
        self.assertIn("same file", str(caught.exception))
        with self.assertRaises(ValueError):
            predump.validate_jobs("not json")


if __name__ == "__main__":
    unittest.main()
