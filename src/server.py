"""
coord-mcp — Central coordination MCP server.

Exposes :
  - checkin              departure gate: declare scope, detect conflicts
  - claim_issue          bind work item to existing GitHub issue
  - claim_new            create a new GitHub issue + bind
  - list_active_work     see what every agent is doing right now
  - checkout             arrival gate: validate diff vs scope, parallel conflicts
  - release              close work item, optionally close GH issue
  - abandon              drop a declared item that won't be done
  - claim_adr_number     atomically allocate the next free ADR number
  - list_adr_allocations cross-repo ADR registry view
  - claim_migration_number   atomically allocate the next free Flyway version
  - list_migration_allocations  cross-repo migration registry view
  - audit_tail           last N audit log entries (debug)

Default transport: streamable-http on 127.0.0.1:8015.
Persistence: SQLite at ~/.coord-mcp/state.db (override via $COORD_MCP_DB).
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from .adr import claim_adr, list_allocations
from .migration import (
    claim_migration,
    list_migration_allocations as _list_migration_allocations,
)
from .checkout import abandon, checkout, release
from .db import connection, init_db
from .work_items import (
    checkin as _checkin,
    claim_issue as _claim_issue,
    claim_new as _claim_new,
    get_work_item,
    list_active_work as _list_active_work,
)


init_db()
mcp = FastMCP("coord-mcp", host="127.0.0.1", port=8015)


# ── Departure gate ───────────────────────────────────────────────────


@mcp.tool()
def checkin(
    repo_path: str,
    title: str,
    scope_files: list[str] | None = None,
    scope_symbols: list[str] | None = None,
    scope_adr_topic: str | None = None,
    milestone_number: int | None = None,
    eta_hours: float | None = None,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Declare intent to work on something. Detects conflicts BEFORE the agent starts.

    Args:
        repo_path: Absolute path to the repo root (where `docs/adr/` lives).
        title: Short description (used as fallback issue title).
        scope_files: List of file paths the agent intends to modify (relative to repo).
        scope_symbols: Code symbol/concept names the agent intends to touch. If the repo
            is indexed by GitNexus, each symbol's downstream blast radius is resolved and
            unioned into scope_files for conflict detection — catches two agents editing
            different files that both depend on the same symbol. Best-effort, degrades
            silently to file-only matching if GitNexus is unavailable.
        scope_adr_topic: If creating an ADR, the topic — coord-mcp will reserve a number.
        milestone_number: GitHub milestone to attach (optional).
        eta_hours: Estimated time to complete (optional).
        agent_id: Identifier (e.g. worktree name) so other agents see who's working.

    Returns:
        work_item_id, scope_symbols_expanded, gitnexus_warnings, conflicts (overlapping
        active work), similar_existing_issues, suggested_action ('REVIEW_CONFLICTS' /
        'CONSIDER_CLAIMING' / 'CREATE_NEW'). Caller then chooses claim_issue/claim_new/abandon.
    """
    return _checkin(
        repo_path=repo_path,
        title=title,
        scope_files=scope_files,
        scope_symbols=scope_symbols,
        scope_adr_topic=scope_adr_topic,
        milestone_number=milestone_number,
        eta_hours=eta_hours,
        agent_id=agent_id,
    )


@mcp.tool()
def claim_issue(work_item_id: str, github_issue_number: int) -> dict[str, Any]:
    """Bind a work item to an EXISTING GitHub issue (assigns @me on GH)."""
    return _claim_issue(work_item_id, github_issue_number)


@mcp.tool()
def claim_new(
    work_item_id: str,
    body: str = "",
    labels: list[str] | None = None,
) -> dict[str, Any]:
    """Create a NEW GitHub issue and bind the work item to it."""
    return _claim_new(work_item_id, body=body, labels=labels)


# ── Arrival gate ─────────────────────────────────────────────────────


@mcp.tool()
def checkout_work(
    work_item_id: str,
    diff_files: list[str] | None = None,
    auto_detect_diff: bool = True,
) -> dict[str, Any]:
    """Arrival gate before merge. Validates scope match, detects parallel conflicts.

    Returns ready_to_merge: bool, warnings: [...], blockers: [...],
    acceptance_criteria_status: {total, checked, unchecked, all_checked},
    open_pr_conflicts_on_files.

    If auto_detect_diff=True (default), runs `git diff --name-only origin/main...HEAD`
    in the work item's repo to get actual changes.
    """
    return checkout(work_item_id, diff_files=diff_files, auto_detect_diff=auto_detect_diff)


@mcp.tool()
def release_work(
    work_item_id: str,
    outcome: str,
    close_github_issue: bool = False,
) -> dict[str, Any]:
    """Finalize a work item. Stores outcome, optionally closes the GH issue with comment."""
    return release(work_item_id, outcome=outcome, close_github_issue=close_github_issue)


@mcp.tool()
def abandon_work(work_item_id: str, reason: str = "") -> dict[str, Any]:
    """Mark a declared/claimed work item as abandoned (e.g. user changed mind)."""
    return abandon(work_item_id, reason=reason)


# ── Visibility ───────────────────────────────────────────────────────


@mcp.tool()
def list_active_work(repo_path: str | None = None) -> list[dict[str, Any]]:
    """List every work item NOT in terminal status. Cross-repo by default."""
    return _list_active_work(repo_path=repo_path)


@mcp.tool()
def get_work(work_item_id: str) -> dict[str, Any] | None:
    """Fetch a single work item's full record."""
    return get_work_item(work_item_id)


# ── ADR registry ─────────────────────────────────────────────────────


@mcp.tool()
def claim_adr_number(
    repo_path: str,
    topic: str,
    work_item_id: str | None = None,
    allocated_to: str | None = None,
    create_skeleton: bool = True,
) -> dict[str, Any]:
    """Atomically allocate the next free ADR number for a repo.

    Scans filesystem (`<repo_path>/docs/adr/`) + DB allocations, picks MAX+1,
    INSERTs with UNIQUE constraint to serialize concurrent callers.
    Optionally writes a minimal ADR skeleton file.

    Returns: adr_number, filename, file_path, slug, created_skeleton, repo, topic.
    """
    return claim_adr(
        repo_path=repo_path,
        topic=topic,
        work_item_id=work_item_id,
        allocated_to=allocated_to,
        create_skeleton=create_skeleton,
    )


@mcp.tool()
def list_adr_allocations(repo_path: str | None = None) -> list[dict[str, Any]]:
    """Show all ADR allocations known to coord-mcp. Filter by repo if given."""
    return list_allocations(repo_path=repo_path)


# ── Flyway migration registry ────────────────────────────────────────


@mcp.tool()
def claim_migration_number(
    repo_path: str,
    description: str,
    migrations_dir: str | None = None,
    work_item_id: str | None = None,
    allocated_to: str | None = None,
    scan_branches: bool = True,
) -> dict[str, Any]:
    """Atomically allocate the next free Flyway migration version for a repo.

    À appeler AVANT d'écrire un fichier `V<n>__*.sql`, comme claim_adr_number pour un ADR.

    Différence avec l'allocation d'ADR : la source de vérité inclut **toutes les refs
    git**, pas seulement le disque. Un numéro est pris dès qu'une branche non mergée le
    porte — invisible depuis le working tree. Sur alert-immo (2026-08-07), `main`
    s'arrêtait à V94 alors que les branches portaient déjà V95 à V98 ; un scan du seul
    disque aurait alloué V95, déjà revendiquée trois fois.

    Quatre sources : disque, refs git (`git log --all`), allocations en base, puis la
    contrainte UNIQUE pour trancher les courses restantes.

    Returns: version, filename, file_path, slug, migrations_dir, max_disk, max_git,
    max_db, holes, git_scan_ok, repo, description.

    `holes` liste les numéros absents partout sous le maximum. Ce ne sont PAS des
    versions réutilisables : elles peuvent être appliquées sur un environnement sans
    fichier en face — vérifier `flyway_schema_history` avant toute décision.

    `git_scan_ok=False` signale que le scan git n'a rien renvoyé (dépôt non git, timeout,
    répertoire inconnu de l'historique) : l'allocation reste sérialisée par la base, mais
    la garantie inter-branches n'est pas acquise.
    """
    return claim_migration(
        repo_path=repo_path,
        description=description,
        migrations_dir=migrations_dir,
        work_item_id=work_item_id,
        allocated_to=allocated_to,
        scan_branches=scan_branches,
    )


@mcp.tool()
def list_migration_allocations(repo_path: str | None = None) -> list[dict[str, Any]]:
    """Show all Flyway migration allocations known to coord-mcp. Filter by repo if given."""
    return _list_migration_allocations(repo_path=repo_path)


# ── Audit ────────────────────────────────────────────────────────────


@mcp.tool()
def audit_tail(limit: int = 20) -> list[dict[str, Any]]:
    """Return the last N audit log entries (most recent first)."""
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# ── Entry point ──────────────────────────────────────────────────────


def main() -> None:
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
