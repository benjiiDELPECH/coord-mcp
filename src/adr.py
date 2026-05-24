"""
ADR allocation logic — atomic next-number across filesystem + DB.

Algorithm:
1. Read filesystem (`<repo_path>/docs/adr/`) for existing `ADR-NNN-*.md`.
2. Read DB (`adr_allocations`) for the same repo_path.
3. Candidate = max(filesystem_max, db_max) + 1.
4. Try INSERT ... candidate. If UNIQUE constraint kicks in (race), recompute and retry.

This handles three failure modes:
- Two coord-mcp tools call concurrently → DB UNIQUE serializes.
- Another agent dropped a file directly (without going through coord-mcp) →
  we re-detect it on retry via filesystem scan.
- DB is fresh / never seen this repo → we bootstrap from filesystem state.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from datetime import date
from pathlib import Path

from .db import connection, now_iso

MAX_RETRIES = 8
ADR_FILE_RE = re.compile(r"^ADR-(\d{3,4})-")


def slugify(text: str) -> str:
    """Conservative kebab-case slug, ASCII-safe."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    return text or "untitled"


def scan_filesystem_max(adr_dir: Path) -> int:
    """Return the highest ADR-NNN number present in adr_dir, or 0 if empty/absent."""
    if not adr_dir.is_dir():
        return 0
    max_num = 0
    for entry in adr_dir.iterdir():
        if not entry.is_file():
            continue
        m = ADR_FILE_RE.match(entry.name)
        if m:
            max_num = max(max_num, int(m.group(1)))
    return max_num


def scan_db_max(conn: sqlite3.Connection, repo_path: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(adr_number), 0) AS m FROM adr_allocations WHERE repo_path = ?",
        (repo_path,),
    ).fetchone()
    return int(row["m"] or 0)


def claim_adr(
    repo_path: str,
    topic: str,
    work_item_id: str | None = None,
    allocated_to: str | None = None,
    create_skeleton: bool = True,
) -> dict:
    """Atomically allocate the next free ADR number for the given repo.

    Args:
        repo_path: Absolute path to the repo root (contains `docs/adr/`).
        topic: Free-form title of the ADR; will be slugified for the filename.
        work_item_id: Optional FK to a work_items row.
        allocated_to: Optional agent identifier.
        create_skeleton: If True, write a minimal ADR file to disk.

    Returns:
        dict with adr_number, filename, file_path, slug, created_skeleton.

    Raises:
        RuntimeError if collision retries exhausted.
    """
    repo = Path(repo_path).resolve()
    adr_dir = repo / "docs" / "adr"
    if not repo.is_dir():
        raise FileNotFoundError(f"Repo path not found: {repo}")
    adr_dir.mkdir(parents=True, exist_ok=True)

    slug = slugify(topic)
    today = date.today().isoformat()

    for attempt in range(MAX_RETRIES):
        fs_max = scan_filesystem_max(adr_dir)

        with connection() as conn:
            db_max = scan_db_max(conn, str(repo))
            candidate = max(fs_max, db_max) + 1
            filename = f"ADR-{candidate:03d}-{slug}.md"
            file_path = adr_dir / filename

            # Refuse if the filesystem already has THIS exact number (covers
            # the "another agent wrote the file between our scan and INSERT"
            # case, where INSERT might still succeed because the DB row is fresh).
            if any(ADR_FILE_RE.match(p.name) and int(ADR_FILE_RE.match(p.name).group(1)) == candidate
                   for p in adr_dir.iterdir() if p.is_file()):
                continue  # retry, scan_filesystem_max will pick it up

            try:
                conn.execute(
                    "INSERT INTO adr_allocations "
                    "(repo_path, adr_number, topic_slug, filename, work_item_id, allocated_to, allocated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (str(repo), candidate, slug, filename, work_item_id, allocated_to, now_iso()),
                )
            except sqlite3.IntegrityError:
                continue  # UNIQUE constraint hit, retry with fresh scan

        created_skeleton = False
        if create_skeleton and not file_path.exists():
            file_path.write_text(_skeleton(candidate, topic, today), encoding="utf-8")
            created_skeleton = True

        return {
            "adr_number": candidate,
            "filename": filename,
            "file_path": str(file_path),
            "slug": slug,
            "created_skeleton": created_skeleton,
            "repo": str(repo),
            "topic": topic,
        }

    raise RuntimeError(
        f"Failed to allocate ADR number after {MAX_RETRIES} attempts (heavy contention?)"
    )


def _skeleton(adr_number: int, topic: str, today: str) -> str:
    return f"""# ADR-{adr_number:03d} — {topic}

**Date** : {today}
**Statut** : Proposé
**Auteurs** :

---

## Contexte

(à rédiger)

## Décision

(à rédiger)

## Conséquences

### Positives

### Négatives / risques

### Neutres

---

## Alternatives écartées

(à rédiger)

## Références

"""


def list_allocations(repo_path: str | None = None) -> list[dict]:
    """Return all known ADR allocations, optionally filtered by repo."""
    with connection() as conn:
        if repo_path:
            rows = conn.execute(
                "SELECT * FROM adr_allocations WHERE repo_path = ? ORDER BY adr_number",
                (str(Path(repo_path).resolve()),),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM adr_allocations ORDER BY repo_path, adr_number"
            ).fetchall()
        return [dict(r) for r in rows]
