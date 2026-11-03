import socket
import tempfile
from pathlib import Path

import pytest
from recorded_fixtures import cleanup, prepare

from paper_review.config import ROOT, Settings
from paper_review.infrastructure.academic_api.s2_request_gate import SharedS2RequestGate
from paper_review.infrastructure.database.models import Base
from paper_review.infrastructure.database.session import make_engine


@pytest.fixture(scope="session", autouse=True)
def recorded_regression_inputs():
    created = prepare(ROOT)
    try:
        yield
    finally:
        cleanup(created)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("network access is forbidden in tests")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)


@pytest.fixture(autouse=True)
def isolated_s2_gate(monkeypatch):
    (ROOT / ".work").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT / ".work") as directory:
        gate = SharedS2RequestGate(Path(directory) / "gate.json", stop_path=None, interval=0)
        monkeypatch.setattr(
            "paper_review.infrastructure.academic_api.semantic_scholar.default_s2_request_gate",
            lambda: gate,
        )
        yield gate


@pytest.fixture
def settings():
    (ROOT / ".work").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT / ".work") as directory:
        yield Settings(database_path=Path(directory) / "test.db")


@pytest.fixture
def engine(settings):
    db = make_engine(settings)
    # Unit/integration isolation only; production startup never calls create_all.
    Base.metadata.create_all(db)
    yield db
    db.dispose()
