"""
Flyway migration version allocation — atomic next-version across filesystem + DB.

Mirrors adr.py exactly (same race, same fix). Born 07.09.2026 after V144 and
V154 each collided twice on alert-immo main (two different agents/sessions
independently picked MAX(V*)+1 from a stale local checkout). The existing
guard, check-migrations-immutable.sh, catches this only at merge time of the
SECOND colliding PR — after CI has already burned a cycle on it. This closes
the race at the source: reserve the version before writing the file.

Algorithm (identical to claim_adr):
1. Read filesystem (`<repo_path>/<migrations_dir>/`) for existing `VNNN__*.sql`.
2. Read DB (`migration_allocations`) for the same repo_path.
3. Candidate = max(filesystem_max, db_max) + 1.
4. Try INSERT ... candidate. If UNIQUE constraint kicks in (race), recompute and retry.
"""

from __future__ import annotations

import logging
import random
import re
import sqlite3
import time
import unicodedata
from pathlib import Path

from .db import connection, now_iso

logger = logging.getLogger(__name__)

MAX_RETRIES = 8
MIGRATION_FILE_RE = re.compile(r"^V(\d+)__")

# Fixed relative to repo_path — this is the one location Flyway migrations
# live in this codebase; no known second repo uses this table, so (unlike
# scope_files in work_items) there's nothing to make configurable yet.
DEFAULT_MIGRATIONS_DIR = "ai-scraping-service/bootstrap/src/main/resources/db/migration"

BACKOFF_BASE_S = 0.01
BACKOFF_FACTOR = 2
BACKOFF_MAX_S = 0.5


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with full jitter — see adr.py._backoff_delay for why
    jitter (not a fixed exponential value) matters when two agents collide on
    the same attempt number.
    """
    cap = min(BACKOFF_MAX_S, BACKOFF_BASE_S * (BACKOFF_FACTOR ** attempt))
    return random.uniform(0, cap)


def slugify(text: str) -> str:
    """Conservative snake_case slug, ASCII-safe, matches Flyway description convention."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = text.strip("_")
    return text or "untitled"


def scan_filesystem_max(migrations_dir: Path) -> int:
    """Return the highest VNNN version present in migrations_dir, or 0 if empty/absent."""
    if not migrations_dir.is_dir():
        return 0
    max_num = 0
    for entry in migrations_dir.iterdir():
        if not entry.is_file():
            continue
        m = MIGRATION_FILE_RE.match(entry.name)
        if m:
            max_num = max(max_num, int(m.group(1)))
    return max_num


def scan_db_max(conn: sqlite3.Connection, repo_path: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(migration_version), 0) AS m FROM migration_allocations WHERE repo_path = ?",
        (repo_path,),
    ).fetchone()
    return int(row["m"] or 0)


def claim_migration(
    repo_path: str,
    description: str,
    migrations_dir: str | None = None,
    work_item_id: str | None = None,
    allocated_to: str | None = None,
    create_skeleton: bool = True,
) -> dict:
    """Atomically allocate the next free Flyway migration version for the given repo.

    Args:
        repo_path: Absolute path to the repo root.
        description: Free-form description of the migration; slugified into the filename
            (Flyway convention: V<n>__<description>.sql).
        migrations_dir: Path to the migrations dir, relative to repo_path. Defaults to
            DEFAULT_MIGRATIONS_DIR.
        work_item_id: Optional FK to a work_items row.
        allocated_to: Optional agent identifier.
        create_skeleton: If True, write an empty migration file with a header comment
            (unlike ADR, there's no meaningful skeleton body — the agent writes real SQL).

    Returns:
        dict with migration_version, filename, file_path, slug, created_skeleton.

    Raises:
        RuntimeError if collision retries exhausted.
    """
    repo = Path(repo_path).resolve()
    if not repo.is_dir():
        raise FileNotFoundError(f"Repo path not found: {repo}")
    mdir = repo / (migrations_dir or DEFAULT_MIGRATIONS_DIR)
    mdir.mkdir(parents=True, exist_ok=True)

    slug = slugify(description)

    for attempt in range(MAX_RETRIES):
        fs_max = scan_filesystem_max(mdir)

        with connection() as conn:
            db_max = scan_db_max(conn, str(repo))
            candidate = max(fs_max, db_max) + 1
            filename = f"V{candidate}__{slug}.sql"
            file_path = mdir / filename

            # Refuse if the filesystem already has THIS exact version (covers the
            # "another agent wrote the file between our scan and INSERT" case).
            if any(
                MIGRATION_FILE_RE.match(p.name) and int(MIGRATION_FILE_RE.match(p.name).group(1)) == candidate
                for p in mdir.iterdir() if p.is_file()
            ):
                logger.warning(
                    "claim_migration: candidate V%d already on disk for %s, retrying (attempt %d/%d)",
                    candidate, repo, attempt + 1, MAX_RETRIES,
                )
                time.sleep(_backoff_delay(attempt))
                continue

            try:
                conn.execute(
                    "INSERT INTO migration_allocations "
                    "(repo_path, migration_version, description_slug, filename, work_item_id, allocated_to, allocated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (str(repo), candidate, slug, filename, work_item_id, allocated_to, now_iso()),
                )
            except sqlite3.IntegrityError:
                logger.warning(
                    "claim_migration: UNIQUE collision on V%d for %s, retrying (attempt %d/%d)",
                    candidate, repo, attempt + 1, MAX_RETRIES,
                )
                time.sleep(_backoff_delay(attempt))
                continue

        created_skeleton = False
        if create_skeleton and not file_path.exists():
            file_path.write_text(_skeleton(candidate, description), encoding="utf-8")
            created_skeleton = True

        return {
            "migration_version": candidate,
            "filename": filename,
            "file_path": str(file_path),
            "slug": slug,
            "created_skeleton": created_skeleton,
            "repo": str(repo),
            "description": description,
        }

    logger.error(
        "claim_migration: exhausted %d attempts for %s — heavy contention or a stuck lock",
        MAX_RETRIES, repo,
    )
    raise RuntimeError(
        f"Failed to allocate migration version after {MAX_RETRIES} attempts (heavy contention?)"
    )


def _skeleton(version: int, description: str) -> str:
    return f"-- V{version}__{slugify(description)}.sql — {description}\n-- (à rédiger)\n"


def list_allocations(repo_path: str | None = None) -> list[dict]:
    """Return all known migration version allocations, optionally filtered by repo."""
    with connection() as conn:
        if repo_path:
            rows = conn.execute(
                "SELECT * FROM migration_allocations WHERE repo_path = ? ORDER BY migration_version",
                (str(Path(repo_path).resolve()),),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM migration_allocations ORDER BY repo_path, migration_version"
            ).fetchall()
        return [dict(r) for r in rows]
