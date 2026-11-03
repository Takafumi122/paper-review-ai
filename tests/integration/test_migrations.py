"""DDL validation is separate from the real Alembic lifecycle check."""

import ast
import importlib.util
import subprocess
import sys

import pytest
from sqlalchemy import inspect

from paper_review.config import ROOT
from paper_review.infrastructure.database.session import make_engine


def test_frozen_migration_ddl(settings):
    tree = ast.parse((ROOT / "migrations/versions/0001_foundation.py").read_text())
    upgrade = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "upgrade"
    )
    engine = make_engine(settings)
    try:
        with engine.begin() as connection:
            for node in upgrade.body:
                assert isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                connection.exec_driver_sql(ast.literal_eval(node.value.args[0]))
            assert connection.exec_driver_sql("PRAGMA integrity_check").scalar() == "ok"
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
        inspector = inspect(engine)
        foundation_tables = {"papers", "analysis_jobs", "stage_executions", "llm_executions"}
        assert set(inspector.get_table_names()) == foundation_tables
        assert {c["name"] for c in inspector.get_columns("papers")} == {"id", "external_ids"}
        assert {c["name"] for c in inspector.get_columns("analysis_jobs")} == {
            "id",
            "paper_id",
            "arxiv_revision",
            "fingerprint",
            "status",
            "current_stage",
            "feature_status",
            "result",
            "error_code",
            "owner",
            "lease_until",
            "heartbeat_at",
            "created_at",
            "started_at",
            "finished_at",
        }
        index = inspector.get_indexes("analysis_jobs")[0]
        assert index["name"] == "uq_active_fingerprint"
        assert index["unique"]
    finally:
        engine.dispose()


def test_alembic_upgrade_downgrade_and_drift(settings):
    if importlib.util.find_spec("alembic") is None:
        pytest.skip("BLOCKED: Alembic is unavailable locally; no network allowed")
    environment = {
        "PAPER_REVIEW_DATABASE_PATH": str(settings.database_path),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for command in [("upgrade", "head"), ("check",), ("downgrade", "base"), ("upgrade", "head")]:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *command],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
