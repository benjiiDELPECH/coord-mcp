#!/usr/bin/env python3
"""Migration SQLite -> PostgreSQL des tables oubliées par la migration 001.

    adr_allocations       147 lignes  — numéros d'ADR déjà alloués
    migration_allocations   1 ligne
    audit_log            5662 lignes  — piste d'audit

Écrit un CSV par table dans le répertoire donné. Le script ÉCHOUE si un
horodatage est illisible plutôt que de le deviner : une date approximative dans
une piste d'audit est pire qu'une migration interrompue.

Usage :
    python3 import_missing_tables.py ~/.coord-mcp/state.db /tmp/coord_pg
"""

from __future__ import annotations

import csv
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

TABLES: dict[str, list[str]] = {
    "adr_allocations": [
        "id", "repo_path", "adr_number", "topic_slug", "filename",
        "work_item_id", "allocated_to", "allocated_at",
    ],
    "migration_allocations": [
        "id", "repo_path", "version", "description_slug", "filename",
        "work_item_id", "allocated_to", "allocated_at",
    ],
    "audit_log": [
        "id", "timestamp", "tool", "args_json", "result_json",
        "agent_id", "work_item_id",
    ],
}

# Colonnes à normaliser en instants UTC.
INSTANT_COLUMNS = {"allocated_at", "timestamp"}


def norm_instant(raw: str | None, table: str, column: str) -> str | None:
    if raw is None or not str(raw).strip():
        return None
    text = str(raw).strip()
    try:
        if "T" not in text and " " in text:
            return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).isoformat()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise SystemExit(f"horodatage illisible {table}.{column} : {raw!r} ({exc})") from exc


def export(con: sqlite3.Connection, table: str, columns: list[str], out_dir: Path) -> int:
    con.row_factory = sqlite3.Row
    rows = con.execute(f"SELECT {', '.join(columns)} FROM {table}").fetchall()

    path = out_dir / f"{table}.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        for row in rows:
            values = []
            for column in columns:
                value = row[column]
                if column in INSTANT_COLUMNS:
                    value = norm_instant(value, table, column)
                values.append("" if value is None else str(value))
            writer.writerow(values)
    return len(rows)


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2

    db_path = Path(sys.argv[1]).expanduser()
    out_dir = Path(sys.argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    for table, columns in TABLES.items():
        count = export(con, table, columns, out_dir)
        print(f"  {table:24} {count:>6} lignes -> {out_dir / (table + '.csv')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
