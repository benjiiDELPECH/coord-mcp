"""
Flyway migration version allocation — atomic next-number across git + filesystem + DB.

Pourquoi ce module existe (incident alert-immo, 2026-08-07)
-----------------------------------------------------------
`claim_adr_number` sérialise les numéros d'ADR, mais rien ne sérialisait les numéros
de migration Flyway. Chaque agent faisait `MAX(V*)+1` sur SA branche. Résultat mesuré
sur 572 branches non mergées :

    V91 → 5 contenus concurrents    V92 → 4    V95 → 3    V85, V87, V90, V93 → 2

Flyway identifie une migration par sa **version**, pas par son nom de fichier. Deux
`V91__*.sql` distincts sur une base où V91 est déjà appliquée = checksum mismatch au
démarrage, donc CrashLoopBackOff.

Différence clé avec l'allocation d'ADR
--------------------------------------
Un ADR se réserve contre le disque local : le fichier est écrit tout de suite, sur main.
Une migration se réserve contre **toutes les branches** : le numéro est pris par du code
qui n'est pas encore mergé, et qui est invisible depuis le répertoire de travail.

Sur alert-immo au 2026-08-07 : `main` s'arrête à V94, mais les branches portent déjà
V95, V96, V97 et V98. Un scan du seul disque aurait alloué V95 — déjà pris trois fois.

D'où quatre sources scannées, et non deux :
  1. le répertoire de migrations du working tree ;
  2. **toutes les refs git** (branches locales et distantes), via un seul `git log --all` ;
  3. les allocations en base ;
  4. la contrainte UNIQUE, qui tranche les courses restantes.

Le module signale aussi les **trous de séquence** : un numéro absent partout peut avoir
été appliqué sur un environnement sans que son fichier existe encore quelque part
(cas mesuré : V91 et V92 appliquées en base de dev, absentes de main et de toute branche
récente). Un trou n'est donc pas une opportunité de réutilisation — c'est un avertissement.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
import unicodedata
from pathlib import Path

from .db import connection, now_iso

MAX_RETRIES = 8

# Flyway : V<version>__<description>.sql — version = entier, ou pointé/souligné (V1_1, V2.3).
MIGRATION_FILE_RE = re.compile(r"^V(\d+)(?:[._]\d+)*__.+\.sql$", re.IGNORECASE)

# Chemins conventionnels, essayés dans l'ordre si `migrations_dir` n'est pas fourni.
CONVENTIONAL_DIRS = (
    "src/main/resources/db/migration",
    "bootstrap/src/main/resources/db/migration",
    "db/migration",
    "migrations",
)

GIT_SCAN_TIMEOUT_S = 30


def slugify(text: str) -> str:
    """Snake_case slug ASCII — convention Flyway pour la partie description."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = text.strip("_")
    return text or "untitled"


def version_of(filename: str) -> int | None:
    """Version majeure d'un nom de fichier Flyway, ou None si le nom ne matche pas."""
    m = MIGRATION_FILE_RE.match(Path(filename).name)
    return int(m.group(1)) if m else None


def find_migrations_dir(repo: Path, explicit: str | None = None) -> Path:
    """Localise le répertoire de migrations.

    Ordre : chemin explicite → chemins conventionnels → recherche du répertoire
    contenant le plus de fichiers `V*__*.sql`. Lève si rien n'est trouvé : mieux vaut
    échouer que d'allouer contre un répertoire vide (ce qui rendrait toute version
    « libre »).
    """
    if explicit:
        d = (repo / explicit).resolve() if not Path(explicit).is_absolute() else Path(explicit)
        if not d.is_dir():
            raise FileNotFoundError(f"migrations_dir introuvable : {d}")
        return d

    for rel in CONVENTIONAL_DIRS:
        for candidate in repo.glob(f"**/{rel}"):
            if candidate.is_dir():
                return candidate

    best: tuple[int, Path] | None = None
    for candidate in repo.glob("**/migration*"):
        if not candidate.is_dir():
            continue
        n = sum(1 for p in candidate.iterdir() if p.is_file() and MIGRATION_FILE_RE.match(p.name))
        if n and (best is None or n > best[0]):
            best = (n, candidate)
    if best:
        return best[1]

    raise FileNotFoundError(
        f"Aucun répertoire de migrations trouvé sous {repo}. "
        "Passez `migrations_dir` explicitement."
    )


def scan_disk(migrations_dir: Path) -> set[int]:
    """Versions présentes dans le working tree."""
    if not migrations_dir.is_dir():
        return set()
    out = set()
    for p in migrations_dir.iterdir():
        if p.is_file():
            v = version_of(p.name)
            if v is not None:
                out.add(v)
    return out


def scan_git(repo: Path, migrations_dir: Path) -> set[int]:
    """Versions présentes sur **n'importe quelle ref** git — c'est le cœur du module.

    Un seul `git log --all` filtré sur le chemin des migrations : bien plus rapide
    qu'un `ls-tree` par branche (836 branches sur alert-immo). Renvoie un ensemble vide
    en cas d'échec — l'appelant est prévenu via `git_scan_ok`, et la contrainte UNIQUE
    reste le dernier rempart.
    """
    try:
        rel = migrations_dir.relative_to(repo)
    except ValueError:
        rel = migrations_dir

    try:
        proc = subprocess.run(
            [
                "git", "-C", str(repo), "log", "--all",
                "--pretty=format:", "--name-only", "--diff-filter=AMR",
                "--", f"{rel}/",
            ],
            capture_output=True, text=True, timeout=GIT_SCAN_TIMEOUT_S, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return set()
    if proc.returncode != 0:
        return set()

    out = set()
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        v = version_of(line)
        if v is not None:
            out.add(v)
    return out


def scan_db(conn: sqlite3.Connection, repo_path: str) -> set[int]:
    rows = conn.execute(
        "SELECT version FROM migration_allocations WHERE repo_path = ?", (repo_path,)
    ).fetchall()
    return {int(r["version"]) for r in rows}


def find_holes(versions: set[int]) -> list[int]:
    """Numéros absents sous le maximum connu.

    Un trou n'est PAS une version réutilisable : elle peut être appliquée sur un
    environnement sans fichier en face (cas alert-immo V91/V92). On les remonte pour
    que l'appelant vérifie `flyway_schema_history` avant toute décision.
    """
    if not versions:
        return []
    return [v for v in range(1, max(versions)) if v not in versions]


def claim_migration(
    repo_path: str,
    description: str,
    migrations_dir: str | None = None,
    work_item_id: str | None = None,
    allocated_to: str | None = None,
    scan_branches: bool = True,
) -> dict:
    """Réserve atomiquement la prochaine version de migration libre pour un dépôt.

    Args:
        repo_path: racine du dépôt.
        description: intitulé libre ; slugifié pour la partie description du nom.
        migrations_dir: chemin du répertoire de migrations (auto-détecté sinon).
        work_item_id: FK optionnelle vers un work item.
        allocated_to: identifiant d'agent.
        scan_branches: scanner toutes les refs git (défaut True — le désactiver
            revient à reproduire le bug que ce module corrige).

    Returns:
        version, filename, file_path, slug, max_disk, max_git, max_db, holes,
        git_scan_ok, repo, description.

    Raises:
        RuntimeError si les tentatives sont épuisées (contention forte).
    """
    repo = Path(repo_path).resolve()
    if not repo.is_dir():
        raise FileNotFoundError(f"Repo path not found: {repo}")

    mig_dir = find_migrations_dir(repo, migrations_dir)
    slug = slugify(description)

    git_versions: set[int] = set()
    git_scan_ok = False
    if scan_branches:
        git_versions = scan_git(repo, mig_dir)
        git_scan_ok = bool(git_versions)

    for _attempt in range(MAX_RETRIES):
        disk_versions = scan_disk(mig_dir)

        with connection() as conn:
            db_versions = scan_db(conn, str(repo))
            known = disk_versions | git_versions | db_versions
            candidate = (max(known) + 1) if known else 1

            # Le fichier a pu apparaître entre le scan et l'INSERT (autre agent
            # écrivant en direct, sans passer par coord-mcp).
            if candidate in scan_disk(mig_dir):
                continue

            filename = f"V{candidate}__{slug}.sql"
            try:
                conn.execute(
                    "INSERT INTO migration_allocations "
                    "(repo_path, version, description_slug, filename, work_item_id, "
                    "allocated_to, allocated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (str(repo), candidate, slug, filename, work_item_id,
                     allocated_to, now_iso()),
                )
            except sqlite3.IntegrityError:
                continue  # UNIQUE (repo_path, version) — course perdue, on rescanne

        return {
            "version": candidate,
            "filename": filename,
            "file_path": str(mig_dir / filename),
            "slug": slug,
            "migrations_dir": str(mig_dir),
            "max_disk": max(disk_versions) if disk_versions else 0,
            "max_git": max(git_versions) if git_versions else 0,
            "max_db": max(db_versions) if db_versions else 0,
            "holes": find_holes(known),
            "git_scan_ok": git_scan_ok,
            "repo": str(repo),
            "description": description,
        }

    raise RuntimeError(
        f"Échec d'allocation après {MAX_RETRIES} tentatives (contention forte ?)"
    )


def list_migration_allocations(repo_path: str | None = None) -> list[dict]:
    """Allocations connues, filtrables par dépôt."""
    with connection() as conn:
        if repo_path:
            rows = conn.execute(
                "SELECT * FROM migration_allocations WHERE repo_path = ? ORDER BY version",
                (str(Path(repo_path).resolve()),),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM migration_allocations ORDER BY repo_path, version"
            ).fetchall()
        return [dict(r) for r in rows]
