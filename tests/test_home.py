"""deployctl's own files under ~/.deployctl: private folders, atomic private files, one writer at a time."""

from __future__ import annotations

import os
import stat
import threading
import time

import pytest

from deployctl.cli import home, paths


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class TestFolders:
    def test_every_folder_below_the_state_home_is_private(self):
        deep = home.ensure_dir(paths.state_home() / "logs" / "panels")
        for folder in (deep, deep.parent, paths.state_home()):
            assert _mode(folder) == 0o700, folder

    def test_nothing_above_the_state_home_is_touched(self, tmp_path):
        before = _mode(tmp_path)
        home.ensure_dir(paths.state_home() / "x")
        assert _mode(tmp_path) == before


class TestPrivateFiles:
    def test_written_owner_only(self):
        path = home.write_private(paths.state_home() / "credentials.env", "TOKEN=x\n")
        assert path.read_text() == "TOKEN=x\n"
        assert _mode(path) == 0o600

    def test_a_readable_file_is_replaced_by_a_private_one(self):
        path = paths.state_home() / "projects.json"
        home.ensure_dir(path.parent)
        path.write_text("old")
        os.chmod(path, 0o644)
        home.write_private(path, "new")
        assert path.read_text() == "new" and _mode(path) == 0o600

    def test_a_failed_write_leaves_the_old_file_and_no_debris(self, monkeypatch):
        path = home.write_private(paths.state_home() / "projects.json", "old")

        def refuse(*_):
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", refuse)
        with pytest.raises(OSError):
            home.write_private(path, "new")
        assert path.read_text() == "old"
        assert [p.name for p in path.parent.iterdir()] == ["projects.json"]


class TestLock:
    def test_one_writer_at_a_time(self):
        path = paths.state_home() / "projects.json"
        order: list[str] = []
        holding = threading.Event()

        def first():
            with home.locked(path):
                order.append("first in")
                holding.set()
                time.sleep(0.3)
                order.append("first out")

        thread = threading.Thread(target=first)
        thread.start()
        holding.wait(5)
        with home.locked(path):
            order.append("second")
        thread.join()
        assert order == ["first in", "first out", "second"]
