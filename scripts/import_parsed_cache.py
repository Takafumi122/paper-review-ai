"""Copy public parsed paper data from an existing local DB, never jobs or credentials.

Use only for a database whose public arXiv retrieval provenance is known. The
original parser version, PDF content hash and references remain unchanged.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from paper_review.config import ROOT, Settings
from paper_review.infrastructure.database.models import (
    Paper,
    PaperRevision,
    PaperSection,
    Reference,
)
from paper_review.infrastructure.database.session import make_engine, transaction


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "paper-review.db")
    parser.add_argument("--destination", type=Path, default=ROOT / ".work/app.db")
    args = parser.parse_args()
    if args.source.resolve() == args.destination.resolve():
        parser.error("source and destination must differ")
    source_path = Settings(database_path=args.source).database_path
    if not source_path.is_file():
        parser.error("source database does not exist")
    source = create_engine(f"sqlite:///file:{source_path}?mode=ro&uri=true")
    dest = make_engine(Settings(database_path=args.destination))
    counts = {}
    accepted_revisions: set[str] = set()
    with Session(source) as reader, transaction(dest) as writer:
        for model in (Paper, PaperRevision, PaperSection, Reference):
            count = 0
            for row in reader.scalars(select(model)):
                if model is PaperRevision and not row.content_hash:
                    continue
                if model is PaperRevision:
                    accepted_revisions.add(row.id)
                if model in (PaperSection, Reference) and row.revision_id not in accepted_revisions:
                    continue
                existing = writer.get(model, row.id)
                if existing is not None:
                    # Do not overwrite current target records, except a paper
                    # placeholder created by a previously failed retrieval.
                    if model is not Paper or getattr(existing, "title", None) is not None:
                        continue
                values = {
                    column.key: getattr(row, column.key) for column in model.__table__.columns
                }
                writer.merge(model(**values))
                writer.flush()
                count += 1
            counts[model.__tablename__] = count
    source.dispose()
    dest.dispose()
    print(counts)


if __name__ == "__main__":
    main()
