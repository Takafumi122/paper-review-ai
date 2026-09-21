import socket
import tempfile
from pathlib import Path

import pytest

from paper_review.config import ROOT, Settings
from paper_review.infrastructure.database.models import Base
from paper_review.infrastructure.database.session import make_engine


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("network access is forbidden in tests")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)


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
