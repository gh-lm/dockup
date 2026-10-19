"""Every service must be compared against its own image.

A three-service stack on a real machine had `compose config --services`
and `compose config --images` come back in different orders, so zipping
them paired each service with another service's image. Nothing ever
matched, the stack was flagged as needing an update for ever, and
updating it changed nothing.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import runtime  # noqa: E402


COMPOSE = {
    "services": {
        "SnapOtter": {"image": "snapotter/snapotter:latest"},
        "postgres": {"image": "postgres:17-alpine"},
        "redis": {"image": "redis:8-alpine"},
        # A service built on the machine has no image to compare against.
        "worker": {"build": {"context": "."}},
    }
}

# What each container is actually running.
RUNNING = {
    "SnapOtter": "sha256:aaa",
    "SnapOtter-postgres": "sha256:bbb",
    "SnapOtter-redis": "sha256:ccc",
}
# What the declared tag resolves to locally.
LOCAL = {
    "snapotter/snapotter:latest": "sha256:aaa",
    "postgres:17-alpine": "sha256:bbb",
    "redis:8-alpine": "sha256:ccc",
}


class FakeEngine:
    """Stands in for the engine, answering the four commands the check makes."""

    def __init__(self, local=None):
        self.local = dict(LOCAL if local is None else local)

    def _run(self, args, timeout=None, cwd=None, compose=False):
        if args[:2] == ["compose", "-p"] and "pull" in args:
            return ""
        if "config" in args and "--format" in args:
            return json.dumps(COMPOSE)
        if "ps" in args:
            rows = [
                {"Name": "SnapOtter", "Service": "SnapOtter"},
                {"Name": "SnapOtter-postgres", "Service": "postgres"},
                {"Name": "SnapOtter-redis", "Service": "redis"},
            ]
            return "\n".join(json.dumps(r) for r in rows)
        if args[0] == "inspect":
            return RUNNING[args[1]]
        if args[:2] == ["image", "inspect"]:
            ref = args[2]
            if ref not in self.local:
                raise runtime.RuntimeError_("no such image")
            return self.local[ref]
        raise AssertionError(f"unexpected command: {args}")


class UpdateCheckPairing(unittest.TestCase):
    def test_up_to_date_stack_is_not_flagged(self):
        engine = FakeEngine()
        available = runtime.Runtime.check_stack_update(engine, "/opt/stacks/snapotter", "snapotter")
        self.assertFalse(available, "every service is on its own declared image")

    def test_a_real_newer_image_is_still_found(self):
        newer = dict(LOCAL)
        newer["redis:8-alpine"] = "sha256:ddd"
        engine = FakeEngine(newer)
        available = runtime.Runtime.check_stack_update(engine, "/opt/stacks/snapotter", "snapotter")
        self.assertTrue(available, "redis has a newer image waiting")


if __name__ == "__main__":
    unittest.main()
