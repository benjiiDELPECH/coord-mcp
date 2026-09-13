#!/usr/bin/env python3
"""Migration SQLite -> PostgreSQL des work items de coord-mcp.

Ce n'est PAS une copie : c'est une CONVERSION, parce que PostgreSQL refuse ce
que SQLite acceptait.

  - horodatages : trois formats coexistent dans la source (ISO+offset, ISO Z,
    et « 2026-08-27 23:21:57 » sans fuseau — une seule ligne). Ils sont
    normalisés en instants UTC. Un horodatage illisible fait ÉCHOUER la
    migration au lieu d'être deviné.
  - triggers_ci : 0/1 -> false/true.
  - statuts inconnus ou champs obligatoires vides : échec explicite.

Sortie : un CSV sur stdout, consommé par `psql \\copy`. Aucune dépendance
externe (stdlib seulement), pour que la migration reste rejouable sans installer
un pilote PostgreSQL.

Usage :
    python3 import_sqlite.py ~/.coord-mcp/state.db > /tmp/coord_pg_import.csv
"""

from __future__ import annotations

import csv
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

COLUMNS = [
    "id", "repo", "title", "scope_files", "scope_adr_topic", "github_issue_number",
    "milestone_number", "branch_name", "agent_id", "status", "eta_hours",
    "manifest_path", "outcome", "created_at", "updated_at", "scope_symbols",
    "scope_symbols_expanded", "triggers_ci", "revision",
]

VALID_STATUSES = {"declared", "claimed", "in_progress", "checked_out", "released", "abandoned"}


def norm_instant(raw: str | None, row_id: str, column: str) -> str | None:
    """Normalise un horodatage. Échoue BRUYAMMENT plutôt que de deviner."""
    if raw is None or not raw.strip():
        return None
    text = raw.strip()
    try:
        if "T" not in text and " " in text:
            # Format SQLite : espace séparateur, aucun fuseau -> supposé UTC.
            return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).isoformat()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise SystemExit(
            f"horodatage illisible pour {row_id}.{column} : {raw!r} ({exc})"
        ) from exc


def norm_bool(raw: object) -> str:
    if raw is None:
        return "false"
    return "true" if str(raw).strip() in ("1", "true", "True", "t") else "false"


def convert(row: sqlite3.Row) -> list[str]:
    row_id = row["id"]

    if row["status"] not in VALID_STATUSES:
        raise SystemExit(f"statut inconnu pour {row_id} : {row['status']!r}")
    if not (row["repo"] or "").strip():
        raise SystemExit(f"repo vide pour {row_id}")
    if not (row["title"] or "").strip():
        raise SystemExit(f"titre vide pour {row_id}")

    values: dict[str, object] = dict(row)
    values["created_at"] = norm_instant(row["created_at"], row_id, "created_at")
    values["updated_at"] = norm_instant(row["updated_at"], row_id, "updated_at")
    values["triggers_ci"] = norm_bool(row["triggers_ci"])
    values["revision"] = row["revision"] if row["revision"] is not None else 1

    # Le domaine l'exige, la base le garantit : on le vérifie ici pour que
    # l'échec nomme la ligne fautive plutôt que de laisser PostgreSQL refuser.
    if row["status"] in ("released", "abandoned") and not (row["outcome"] or "").strip():
        raise SystemExit(f"{row_id} est {row['status']} sans outcome — invariant violé")

    return ["" if values[c] is None else str(values[c]) for c in COLUMNS]


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2

    db_path = Path(sys.argv[1]).expanduser()
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM work_items ORDER BY created_at, id").fetchall()

    writer = csv.writer(sys.stdout, lineterminator="\n")
    for row in rows:
        writer.writerow(convert(row))

    print(f"migré {len(rows)} lignes", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
