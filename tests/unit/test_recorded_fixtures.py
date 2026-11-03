import shutil

from recorded_fixtures import cleanup, prepare

from paper_review.config import ROOT


def test_fixture_setup_never_replaces_existing_data_and_cleans_only_its_own_files(settings):
    root = settings.database_path.parent
    fixture = root / "tests/fixtures/recorded-context.json.gz"
    fixture.parent.mkdir(parents=True)
    shutil.copy2(ROOT / "tests/fixtures/recorded-context.json.gz", fixture)
    work = root / ".work"
    work.mkdir()
    existing = work / "app.db"
    existing.write_bytes(b"existing application data")
    stop = work / "s2-rate-limited.json"
    stop.write_bytes(b"existing circuit stop")
    created = prepare(root)
    assert existing not in created and stop not in created
    assert existing.read_bytes() == b"existing application data"
    cleanup(created)
    assert existing.read_bytes() == b"existing application data"
    assert stop.read_bytes() == b"existing circuit stop"
    assert all(not p.exists() for p in created)
