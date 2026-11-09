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


def test_model_preparation_is_explicit_and_pinned(tmp_path, monkeypatch):
    from paper_review.infrastructure import local_specter2

    prepare = MODULE["prepare_model"]
    calls = []
    monkeypatch.setenv("PAPER_REVIEW_PREPARE_LOCAL_SPECTER2", "true")
    monkeypatch.setattr(prepare.__globals__["subprocess"], "run",
                        lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(local_specter2, "file_hash",
                        lambda _: MODULE["MODEL_MANIFEST_SHA256"])
    verified = []
    monkeypatch.setattr(local_specter2, "verified_bundle", lambda p: verified.append(p))
    prepare(tmp_path)
    assert calls[0][1] == {"check": True, "timeout": 240}
    command = calls[0][0][0]
    assert command[-1] == "--download"
    assert "3447645e1def9117997203454fa4495937bfbd83" in command
    assert "2081559630a80fc5851d8f798a05ba81e9468089" in command
    assert verified == [tmp_path / ".work/models/specter2/manifest.json"]


def test_existing_deployment_model_is_verified_without_download(tmp_path, monkeypatch):
    from paper_review.infrastructure import local_specter2

    manifest = tmp_path / ".work/models/specter2/manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}")
    monkeypatch.setenv("PAPER_REVIEW_PREPARE_LOCAL_SPECTER2", "true")
    prepare = MODULE["prepare_model"]
    monkeypatch.setattr(prepare.__globals__["subprocess"], "run",
                        lambda *a, **kw: pytest.fail("must not download existing model"))
    monkeypatch.setattr(local_specter2, "file_hash", lambda _: "wrong-hash")
    with pytest.raises(RuntimeError, match="deployment_model_manifest_mismatch"):
        prepare(tmp_path)
    monkeypatch.setattr(local_specter2, "file_hash",
                        lambda _: MODULE["MODEL_MANIFEST_SHA256"])
    monkeypatch.setattr(local_specter2, "verified_bundle", lambda _: None)
    prepare(tmp_path)


def test_model_preparation_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("PAPER_REVIEW_PREPARE_LOCAL_SPECTER2", raising=False)
    MODULE["prepare_model"](tmp_path)
    assert not (tmp_path / ".work").exists()


@pytest.mark.parametrize("reading_only", [None, "true", "", "typo", "false", "0", "no", "off"])
def test_launcher_only_enables_candidates_on_explicit_false(monkeypatch, reading_only):
    main = MODULE["main"]
    monkeypatch.setitem(main.__globals__, "prepare_volume", lambda _: None)
    if reading_only is None:
        monkeypatch.delenv("PAPER_REVIEW_READING_ONLY", raising=False)
    else:
        monkeypatch.setenv("PAPER_REVIEW_READING_ONLY", reading_only)
    monkeypatch.setenv("PAPER_REVIEW_ACADEMIC_BACKEND", "openalex_local")
    monkeypatch.setenv("PAPER_REVIEW_OPENALEX_LOCAL_VERIFIED", "false")
    monkeypatch.setenv("PAPER_REVIEW_OPENALEX_LOCAL_AUDITED", "false")
    calls = []
    os_module = main.__globals__["os"]
    monkeypatch.setattr(os_module, "execv", lambda executable, args: calls.append(args))
    main()
    assert len(calls) == 1
    if reading_only in {"false", "0", "no", "off"}:
        assert calls[0][1:] == ["-m", "paper_review.server", "--host", "0.0.0.0"]
    else:
        assert calls[0][1].endswith("deployment/start_demo.py")
    assert os_module.environ["PAPER_REVIEW_OPENALEX_LOCAL_VERIFIED"] == "false"
    assert os_module.environ["PAPER_REVIEW_OPENALEX_LOCAL_AUDITED"] == "false"
