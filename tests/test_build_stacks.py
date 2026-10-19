"""Updating a stack that builds its own image instead of fetching one.

Not everything self-hosted belongs in a registry. A stack can hold a
company's books and be built on the machine that runs it. Update used to go
looking for those on Docker Hub and come back with "access denied"; once the
compose file was corrected to say `build:`, Update stopped failing and also
stopped doing anything at all.
"""

from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def stacks(monkeypatch, tmp_path):
    from app import stacks as module
    monkeypatch.setattr(module, "compose_path",
                        lambda name: tmp_path / name / "docker-compose.yml")
    return module


def _write(tmp_path, name, text):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "docker-compose.yml").write_text(text)
    return d


def test_a_stack_that_builds_is_recognised(stacks, tmp_path):
    _write(tmp_path, "money", """
services:
  app:
    image: lightmorphic/money:latest
    build: .
    pull_policy: build
""")
    assert stacks._builds_its_own_image("money") is True


def test_a_stack_that_only_fetches_is_not(stacks, tmp_path):
    _write(tmp_path, "plex", """
services:
  plex:
    image: plexinc/pms-docker:latest
""")
    assert stacks._builds_its_own_image("plex") is False


def test_a_missing_or_broken_compose_file_is_not_guessed_at(stacks, tmp_path):
    assert stacks._builds_its_own_image("nothing-here") is False
    _write(tmp_path, "broken", "services: [this is not: valid: yaml")
    assert stacks._builds_its_own_image("broken") is False


def test_a_folder_that_is_not_a_clone_is_left_alone(stacks, tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    assert list(stacks._fetch_source("plain", d)) == []


def test_a_clone_is_brought_up_to_date_before_building(stacks, tmp_path, monkeypatch):
    d = tmp_path / "money"
    (d / ".git").mkdir(parents=True)
    seen = {}

    def fake_run(args, **kw):
        seen["args"] = args
        seen["cwd"] = kw.get("cwd")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(stacks.subprocess, "run", fake_run)
    out = "".join(stacks._fetch_source("money", d))

    assert "pull" in seen["args"] and "--ff-only" in seen["args"]
    assert seen["cwd"] == str(d)
    assert "up to date" in out.lower()


def test_code_edited_in_place_stops_the_fetch_rather_than_merging(
        stacks, tmp_path, monkeypatch):
    """A merge nobody asked for is worse than a line saying why it stopped."""
    d = tmp_path / "money"
    (d / ".git").mkdir(parents=True)
    monkeypatch.setattr(stacks.subprocess, "run", lambda args, **kw:
                        types.SimpleNamespace(returncode=1, stdout="",
                                              stderr="fatal: Not possible to fast-forward"))
    out = "".join(stacks._fetch_source("money", d))
    assert "could not fetch" in out.lower()
    assert "building what is already here" in out.lower()


def test_an_unreachable_repository_still_lets_the_rebuild_happen(
        stacks, tmp_path, monkeypatch):
    """The rebuild is also what keeps the base image patched."""
    d = tmp_path / "money"
    (d / ".git").mkdir(parents=True)

    def boom(args, **kw):
        raise subprocess.TimeoutExpired(args, 120)

    monkeypatch.setattr(stacks.subprocess, "run", boom)
    out = "".join(stacks._fetch_source("money", d))
    assert "building what is already here" in out.lower()


def test_build_refreshes_the_base_image_underneath():
    """Most of a rebuild's security value is in the base image, not the app."""
    from app.runtime import COMPOSE_ACTIONS
    assert COMPOSE_ACTIONS["build"] == ["build", "--pull"]
