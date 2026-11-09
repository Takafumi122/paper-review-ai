"""Workspace-local SQLite cache for alternative retrieval and embedding artifacts."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from paper_review.config import ROOT


class LocalArtifactCache:
    def __init__(self, path: Path):
        self.path = path.resolve()
        if not self.path.is_relative_to(ROOT) or self.path.suffix != ".db":
            raise ValueError("cache_must_be_workspace_database")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS artifacts "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL, expires REAL)"
            )

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def get(self, key: str) -> dict | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM artifacts WHERE key=? AND (expires IS NULL OR expires>?)",
                (key, time.time()),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, value: dict, *, ttl: float | None = None) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO artifacts(key,value,expires) VALUES (?,?,?)",
                (key, json.dumps(value, allow_nan=False), time.time() + ttl if ttl else None),
            )

    def current_values(self, prefix: str) -> list[dict]:
        """Read live entries in one namespace without reviving expired metadata."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT value FROM artifacts WHERE substr(key,1,?)=? "
                "AND (expires IS NULL OR expires>?) ORDER BY key",
                (len(prefix), prefix, time.time()),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def reserve_request(self, key: str, limit: int) -> bool:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT value FROM artifacts WHERE key=?", (key,)).fetchone()
            state = json.loads(row[0]) if row else {"requests": 0, "stopped": False}
            if state["stopped"] or state["requests"] >= limit:
                return False
            state["requests"] += 1
            connection.execute(
                "INSERT OR REPLACE INTO artifacts(key,value,expires) VALUES (?,?,NULL)",
                (key, json.dumps(state)),
            )
        return True

    def stop_requests(self, key: str) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT value FROM artifacts WHERE key=?", (key,)).fetchone()
            state = json.loads(row[0]) if row else {"requests": 0}
            state["stopped"] = True
            connection.execute(
                "INSERT OR REPLACE INTO artifacts(key,value,expires) VALUES (?,?,NULL)",
                (key, json.dumps(state)),
            )
