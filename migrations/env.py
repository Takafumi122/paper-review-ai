from alembic import context

from paper_review.config import Settings
from paper_review.infrastructure.database.models import Base
from paper_review.infrastructure.database.session import make_engine

engine = make_engine(Settings.from_environment())
with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
