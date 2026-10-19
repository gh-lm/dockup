"""Compose-style variable substitution. Run from the repo root with
`.venv/bin/python -m unittest discover tests`."""

import unittest

from app import envsub, hostcompanion


class Substitute(unittest.TestCase):
    def sub(self, text, env=None):
        return envsub.substitute(text, env or {})

    def test_plain(self):
        self.assertEqual(self.sub("${A}/x", {"A": "1"}), "1/x")
        self.assertEqual(self.sub("$A/x", {"A": "1"}), "1/x")
        self.assertEqual(self.sub("${A}", {"A": ""}), "")

    def test_plain_unset_left_as_is(self):
        self.assertEqual(self.sub("${NOPE}/x"), "${NOPE}/x")
        self.assertEqual(self.sub("$NOPE/x"), "$NOPE/x")

    def test_colon_dash(self):
        self.assertEqual(self.sub("${A:-d}", {"A": "1"}), "1")
        self.assertEqual(self.sub("${A:-d}", {"A": ""}), "d")
        self.assertEqual(self.sub("${A:-d}"), "d")

    def test_dash(self):
        self.assertEqual(self.sub("${A-d}", {"A": "1"}), "1")
        self.assertEqual(self.sub("${A-d}", {"A": ""}), "")
        self.assertEqual(self.sub("${A-d}"), "d")

    def test_empty_default(self):
        self.assertEqual(self.sub("x${A:-}y"), "xy")
        self.assertEqual(self.sub("x${A-}y"), "xy")

    def test_path_default(self):
        self.assertEqual(self.sub("${BASE:-/opt/x}/data"), "/opt/x/data")

    def test_dollar_escape(self):
        self.assertEqual(self.sub("a$$b"), "a$b")
        self.assertEqual(self.sub("$${A}", {"A": "1"}), "${A}")
        self.assertEqual(self.sub("$$A", {"A": "1"}), "$A")


class PublishedPorts(unittest.TestCase):
    COMPOSE = 'services:\n  web:\n    ports:\n      - "127.0.0.1:${PANEL_PORT:-4180}:8080"\n'

    def test_default_port(self):
        self.assertEqual(hostcompanion.published_ports(self.COMPOSE), [4180])
        self.assertEqual(hostcompanion.published_ports(self.COMPOSE, "OTHER=1\n"), [4180])

    def test_env_overrides_default(self):
        self.assertEqual(hostcompanion.published_ports(self.COMPOSE, "PANEL_PORT=5000\n"), [5000])

    def test_empty_env_uses_default(self):
        self.assertEqual(hostcompanion.published_ports(self.COMPOSE, "PANEL_PORT=\n"), [4180])


if __name__ == "__main__":
    unittest.main()
