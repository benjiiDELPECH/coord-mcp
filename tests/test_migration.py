"""Tests d'allocation de version Flyway.

Le test qui compte est `test_scan_git_voit_les_branches_non_mergees` : c'est le cas qui
a motivé le module. Un scan du seul disque alloue un numéro déjà pris par une branche.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.migration import (
    find_holes,
    find_migrations_dir,
    scan_disk,
    scan_git,
    slugify,
    version_of,
)


@pytest.fixture
def mig(temp_db):
    """Module migration rechargé sur la base isolée du test."""
    import src.migration as _m
    return _m


# ── helpers ──────────────────────────────────────────────────────────


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    ).stdout


def make_repo(tmp_path: Path, migrations: list[str]) -> tuple[Path, Path]:
    """Dépôt git minimal avec un répertoire de migrations conventionnel."""
    repo = tmp_path / "repo"
    mig_dir = repo / "src" / "main" / "resources" / "db" / "migration"
    mig_dir.mkdir(parents=True)
    for name in migrations:
        (mig_dir / name).write_text("-- noop\n")
    git(repo.parent, "init", "-q", str(repo))
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    return repo, mig_dir


# ── unités ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name,expected",
    [
        ("V91__create_dossier_documents.sql", 91),
        ("V1__Initial_schema.sql", 1),
        ("V30_1__invalidate_all.sql", 30),
        ("V2.3__pointe.sql", 2),
        ("R__repeatable.sql", None),
        ("README.md", None),
        ("V91_pas_de_double_underscore.sql", None),
    ],
)
def test_version_of(name, expected):
    assert version_of(name) == expected


def test_slugify():
    assert slugify("Créer la table dossier_documents !") == "creer_la_table_dossier_documents"
    assert slugify("") == "untitled"


def test_find_holes():
    assert find_holes({1, 2, 3}) == []
    assert find_holes({1, 3, 5}) == [2, 4]
    assert find_holes(set()) == []
    # Cas alert-immo : V91/V92 manquantes sous un max de 94.
    assert find_holes({90, 93, 94}) == list(range(1, 90)) + [91, 92]


# ── le cas qui a motivé le module ────────────────────────────────────


def test_scan_git_voit_les_branches_non_mergees(tmp_path, mig):
    """Reproduction de l'incident alert-immo du 2026-08-07.

    main s'arrête à V94, une branche non mergée porte V95. Le disque ne voit que V94 ;
    seul le scan git voit V95. Sans lui, on allouerait V95 — déjà pris.
    """
    repo, mig_dir = make_repo(tmp_path, ["V93__a.sql", "V94__b.sql"])

    git(repo, "checkout", "-qb", "feature")
    (mig_dir / "V95__deja_pris_ailleurs.sql").write_text("-- noop\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "feature")
    git(repo, "checkout", "-q", "-")  # retour sur main : le fichier disparaît du disque

    assert scan_disk(mig_dir) == {93, 94}, "le disque ne voit pas la branche"
    assert 95 in scan_git(repo, mig_dir), "le scan git doit voir la branche non mergée"

    res = mig.claim_migration(str(repo), "nouvelle table", allocated_to="test")
    assert res["version"] == 96, f"attendu V96 (V95 pris par une branche), obtenu V{res['version']}"
    assert res["max_disk"] == 94
    assert res["max_git"] == 95
    assert res["git_scan_ok"] is True


def test_sans_scan_branches_le_bug_revient(tmp_path, mig):
    """Contrôle négatif : scan_branches=False reproduit exactement le bug corrigé."""
    repo, mig_dir = make_repo(tmp_path, ["V93__a.sql", "V94__b.sql"])
    git(repo, "checkout", "-qb", "feature")
    (mig_dir / "V95__deja_pris.sql").write_text("-- noop\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "f")
    git(repo, "checkout", "-q", "-")

    res = mig.claim_migration(str(repo), "x", scan_branches=False, allocated_to="test")
    assert res["version"] == 95, "sans scan git, on réalloue le numéro déjà pris"
    assert res["git_scan_ok"] is False


# ── allocation ───────────────────────────────────────────────────────


def test_allocation_incrementale_et_persistee(tmp_path, mig):
    repo, _ = make_repo(tmp_path, ["V1__init.sql"])
    a = mig.claim_migration(str(repo), "premiere", allocated_to="agent-a")
    b = mig.claim_migration(str(repo), "seconde", allocated_to="agent-b")
    assert (a["version"], b["version"]) == (2, 3), "la base doit tenir le compte"
    assert a["filename"] == "V2__premiere.sql"

    allocs = mig.list_migration_allocations(str(repo))
    assert [r["version"] for r in allocs] == [2, 3]
    assert {r["allocated_to"] for r in allocs} == {"agent-a", "agent-b"}


def test_repo_vide_commence_a_1(tmp_path, mig):
    repo, _ = make_repo(tmp_path, [])
    assert mig.claim_migration(str(repo), "toute_premiere")["version"] == 1


def test_holes_remontes(tmp_path, mig):
    repo, _ = make_repo(tmp_path, ["V1__a.sql", "V4__b.sql"])
    res = mig.claim_migration(str(repo), "x")
    assert res["version"] == 5
    assert res["holes"] == [2, 3], "les trous doivent être signalés, pas réutilisés"


def test_migrations_dir_explicite(tmp_path, mig):
    repo = tmp_path / "r"
    custom = repo / "sql"
    custom.mkdir(parents=True)
    (custom / "V7__x.sql").write_text("-- noop\n")
    git(repo.parent, "init", "-q", str(repo))
    res = mig.claim_migration(str(repo), "y", migrations_dir="sql")
    assert res["version"] == 8


def test_repertoire_introuvable_leve(tmp_path):
    repo = tmp_path / "vide"
    repo.mkdir()
    with pytest.raises(FileNotFoundError):
        find_migrations_dir(repo)


def test_repo_absent_leve(tmp_path, mig):
    with pytest.raises(FileNotFoundError):
        mig.claim_migration(str(tmp_path / "nexiste_pas"), "x")
