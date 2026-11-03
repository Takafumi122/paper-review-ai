"""Uvicorn entrypoint; schema changes are applied explicitly through Alembic."""

from paper_review.api.app import create_app

app = create_app()
