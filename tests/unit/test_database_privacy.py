import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from paper_review.infrastructure.database.models import Paper


def test_database_exceptions_do_not_echo_paper_text(engine):
    with Session(engine) as session:
        session.add(Paper(id="same-id", external_ids={}))
        session.commit()
        session.add(Paper(id="same-id", abstract="private-paper-text-marker", external_ids={}))
        with pytest.raises(IntegrityError) as error:
            session.commit()
        assert "private-paper-text-marker" not in str(error.value)
        assert "parameters hidden" in str(error.value)
