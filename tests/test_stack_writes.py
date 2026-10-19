"""Saving a stack whose folder Dockup is not allowed to write. Run from
the repo root with `python -m unittest discover tests`."""

import os
import tempfile
import unittest
from pathlib import Path

from app.stacks import FileNotWritable, make_stack_dir, write_stack_file


class WriteStackFile(unittest.TestCase):
    def test_ordinary_write_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "compose.yaml"
            write_stack_file(p, "services: {}\n")
            self.assertEqual(p.read_text(), "services: {}\n")

    @unittest.skipIf(os.getuid() == 0, "root is allowed to write anything")
    def test_read_only_folder_explains_itself(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "nextup"
            d.mkdir()
            os.chmod(d, 0o555)
            try:
                with self.assertRaises(FileNotWritable) as caught:
                    write_stack_file(d / "compose.yaml", "services: {}\n")
            finally:
                os.chmod(d, 0o755)
            message = str(caught.exception)
            self.assertIn("compose.yaml", message)
            self.assertIn(f"user {os.getuid()}", message)
            self.assertIn("chown", message)
            self.assertIn(str(d), message)

    @unittest.skipIf(os.getuid() == 0, "root is allowed to write anything")
    def test_folder_that_cannot_be_made_explains_itself(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "stacks"
            parent.mkdir()
            os.chmod(parent, 0o555)
            try:
                with self.assertRaises(FileNotWritable) as caught:
                    make_stack_dir(parent / "newstack")
            finally:
                os.chmod(parent, 0o755)
            self.assertIn("chown", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
