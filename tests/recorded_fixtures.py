"""Reconstruct the minimum recorded inputs in a fresh checkout, never overwrite local data."""

import base64
import gzip
import json
import sqlite3
from pathlib import Path

from paper_review.config import Settings
from paper_review.infrastructure.database import models as db
from paper_review.infrastructure.database.session import make_engine, transaction


def prepare(root: Path) -> list[Path]:
    created = []
    fixture = json.loads(gzip.decompress(
        (root / "tests/fixtures/recorded-context.json.gz").read_bytes()
    ))
    work = root / ".work"
    work.mkdir(exist_ok=True)
    app_database = work / "app.db"
    if not app_database.exists():
        created.append(app_database)
        engine = make_engine(Settings(database_path=app_database))
        db.Base.metadata.create_all(engine)
        try:
            with transaction(engine) as session:
                for pid in sorted({j["paper_id"] for j in fixture["jobs"]}):
                    session.add(db.Paper(id=pid, external_ids={}))
                session.flush()
                session.add_all(db.PaperRevision(**r) for r in fixture["revisions"])
                session.flush()
                session.add_all(db.PaperSection(**s) for s in fixture["sections"])
                session.add_all(db.AnalysisJob(**j) for j in fixture["jobs"])
        finally:
            engine.dispose()
    for name, rows in fixture["ledgers"].items():
        if Path(name).name != name or not name.endswith(".db") or name == "app.db":
            raise ValueError("invalid recorded ledger path")
        path = work / name
        if path.exists():
            continue
        created.append(path)
        with sqlite3.connect(path) as connection:
            connection.execute(
                "create table llm_executions(cost_status text, reserved_usd real, "
                "estimated_cost_usd real, logical_call_id text, status text, output text)"
            )
            connection.executemany("insert into llm_executions values(?,?,?,?,?,?)", rows)
    for relative, encoded in fixture["files"].items():
        if relative not in {
            ".work/s2-http-gate.json", ".work/s2-rate-limited.json",
            ".work/live-specter2-contract.json", ".work/limited-s2-lora-inputs-2026-11-03.json",
        }:
            raise ValueError("invalid recorded fixture path")
        path = root / relative
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as handle:
                handle.write(base64.b64decode(encoded))
            created.append(path)
    recorded_reports = root / "tests/fixtures/evaluation"
    for source in recorded_reports.rglob("*"):
        if source.is_file():
            path = root / "evaluation/reports" / source.relative_to(recorded_reports)
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("xb") as handle:
                    handle.write(source.read_bytes())
                created.append(path)
    return created


def cleanup(created: list[Path]) -> None:
    for path in reversed(created):
        if path.suffix == ".db":
            for suffix in ("-wal", "-shm"):
                path.with_name(path.name + suffix).unlink(missing_ok=True)
        path.unlink(missing_ok=True)
