"""Volume initialization must drop root before running the application."""

import runpy
from pathlib import Path

import pytest

MODULE = runpy.run_path(str(Path(__file__).resolve().parents[2] / "deployment/start_railway.py"))


def test_root_only_changes_mount_root_and_drops_groups_first(tmp_path, monkeypatch):
    prepare = MODULE["prepare_volume"]
    calls = []
    identity = [0]
    os_module = prepare.__globals__["os"]
    monkeypatch.setattr(os_module, "getuid", lambda: identity[0])
    monkeypatch.setattr(os_module, "chown", lambda *args: calls.append(("chown", *args)))
    monkeypatch.setattr(os_module, "chmod", lambda *args: calls.append(("chmod", *args)))
    monkeypatch.setattr(os_module, "setgroups", lambda value: calls.append(("groups", value)))
    monkeypatch.setattr(os_module, "setgid", lambda value: calls.append(("gid", value)))

    def setuid(value):
        calls.append(("uid", value))
        identity[0] = value

    monkeypatch.setattr(os_module, "setuid", setuid)
    monkeypatch.setattr(os_module, "access", lambda *args: True)
    prepare(tmp_path)
    assert calls == [("chown", tmp_path, 10001, 10001), ("chmod", tmp_path, 0o700),
                     ("groups", []), ("gid", 10001), ("uid", 10001)]


def test_unwritable_or_wrong_user_fails_closed(tmp_path, monkeypatch):
    prepare = MODULE["prepare_volume"]
    os_module = prepare.__globals__["os"]
    monkeypatch.setattr(os_module, "getuid", lambda: 10001)
    monkeypatch.setattr(os_module, "access", lambda *args: False)
    with pytest.raises(RuntimeError, match="app_user_cannot_write_volume"):
        prepare(tmp_path)


def test_symlink_mount_is_refused(tmp_path):
    link = tmp_path / "link"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(RuntimeError, match="symlink"):
        MODULE["prepare_volume"](link)
