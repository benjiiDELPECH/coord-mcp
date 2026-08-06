"""
coord-mcp — SQLite state layer.

Single-writer, multi-reader. Atomic ADR number allocation via UNIQUE constraint
+ retry-on-conflict loop. WAL mode enabled for read concurrency.

State file: ~/.coord-mcp/state.db (configurable via COORD_MCP_DB env var).
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_DB_PATH = Path(os.environ.get("COORD_MCP_DB", str(Path.home() / ".coord-mcp" / "state.db")))


SCHEMA = """
CREATE TABLE IF NOT EXISTS work_items (
    id                  TEXT PRIMARY KEY,
    repo                TEXT NOT NULL,
    title               TEXT NOT NULL,
    scope_files         TEXT,
    scope_symbols       TEXT,
    scope_symbols_expanded TEXT,
    scope_adr_topic     TEXT,
    github_issue_number INTEGER,
    milestone_number    INTEGER,
    branch_name         TEXT,
    agent_id            TEXT,
    status              TEXT NOT NULL CHECK (status IN ('declared','claimed','in_progress','checked_out','released','abandoned')),
    eta_hours           REAL,
    manifest_path       TEXT,
    outcome             TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_work_items_status ON work_items(status);
CREATE INDEX IF NOT EXISTS idx_work_items_repo ON work_items(repo);
CREATE INDEX IF NOT EXISTS idx_work_items_agent ON work_items(agent_id);

CREATE TABLE IF NOT EXISTS adr_allocations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_path       TEXT NOT NULL,
    adr_number      INTEGER NOT NULL,
    topic_slug      TEXT NOT NULL,
    filename        TEXT NOT NULL,
    work_item_id    TEXT,
    allocated_to    TEXT,
    allocated_at    TEXT NOT NULL,
    UNIQUE (repo_path, adr_number)
);

CREATE INDEX IF NOT EXISTS idx_adr_repo ON adr_allocations(repo_path);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,
    tool        TEXT NOT NULL,
    args_json   TEXT,
    result_json TEXT,
    agent_id    TEXT,
    work_item_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_tool ON audit_log(tool);
"""


def now_iso() -> str:
    """UTC ISO-8601 timestamp."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


WORK_ITEMS_COLUMNS = {
    "scope_symbols": "TEXT",
    "scope_symbols_expanded": "TEXT",
}


def _migrate_work_items_columns(conn: sqlite3.Connection) -> None:
    """Add any WORK_ITEMS_COLUMNS missing on an existing table (additive, idempotent)."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(work_items)")}
    for column, sql_type in WORK_ITEMS_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE work_items ADD COLUMN {column} {sql_type}")


def init_db(db_path: Path = DEFAULT_DB_PATH) -> None:
    """Create the DB and schema if not present. Enable WAL for read concurrency."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        _migrate_work_items_columns(conn)
        conn.commit()


@contextmanager
def connection(db_path: Path = DEFAULT_DB_PATH):
    """Context manager for a SQLite connection with sensible defaults."""
    conn = sqlite3.connect(db_path, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def log_audit(
    tool: str,
    args: dict | None = None,
    result: dict | None = None,
    agent_id: str | None = None,
    work_item_id: str | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    """Append an audit log entry. Best-effort, never raises into the caller."""
    import json

    try:
        with connection(db_path) as conn:
            conn.execute(
                "INSERT INTO audit_log (timestamp, tool, args_json, result_json, agent_id, work_item_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    now_iso(),
                    tool,
                    json.dumps(args, default=str) if args else None,
                    json.dumps(result, default=str) if result else None,
                    agent_id,
                    work_item_id,
                ),
            )
    except Exception:
        pass
