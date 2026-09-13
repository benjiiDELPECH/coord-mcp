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
  - audit_tail           last N audit log entries (debug)

Default transport: streamable-http on 127.0.0.1:8015.
Persistence: SQLite at ~/.coord-mcp/state.db (override via $COORD_MCP_DB).
"""

from __future__ import annotations

import asyncio
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import gitnexus_bridge
from .adr import claim_adr, list_allocations
from .checkout import abandon, checkout, release
from .db import connection, init_db
from .work_items import (
    checkin as _checkin,
    claim_issue as _claim_issue,
    claim_new as _claim_new,
    get_work_item,
    list_active_work as _list_active_work,
    plan_parallel_waves as _plan_parallel_waves,
    relink_issue as _relink_issue,
    set_scope_resolver,
)


init_db()
# Composition root: work_items.py only knows the ScopeResolver contract
# (scope_resolver.py) — GitNexus is wired here as the default implementation,
# swappable without touching conflict-detection logic.
set_scope_resolver(gitnexus_bridge.expand_scope)
mcp = FastMCP("coord-mcp", host="127.0.0.1", port=8015)


# ── Departure gate ───────────────────────────────────────────────────


@mcp.tool()
async def checkin(
    repo_path: str,
    title: str,
    scope_files: list[str] | None = None,
    scope_symbols: list[str] | None = None,
    scope_adr_topic: str | None = None,
    milestone_number: int | None = None,
    eta_hours: float | None = None,
    agent_id: str | None = None,
    triggers_ci: bool | None = None,
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
        triggers_ci: Does this item's eventual checkout drive a heavy CI run (compile +
            test, PR push, runner poll)? If omitted, auto-detected from scope_files
            (`.github/workflows/`, compiled/tested source extensions → True). Pass
            explicitly to override the guess. Feeds the GLOBAL (cross-repo) CI-concurrency
            gate — see `ci_gate` in the return value and `checkout_work`'s docstring.

    Returns:
        work_item_id, scope_symbols_expanded, gitnexus_warnings, conflicts (overlapping
        active work), similar_existing_issues, suggested_action ('REVIEW_CONFLICTS' /
        'CONSIDER_CLAIMING' / 'CREATE_NEW'). Caller then chooses claim_issue/claim_new/abandon.

        Also: triggers_ci (bool, resolved value) and ci_gate — a SEPARATE signal from
        `resolution` (which is about scope conflicts). ci_gate.you_should is 'PROCEED'
        or 'WAIT'. 'WAIT' means the shared CI runner (Forgejo, cross alert-immo AND
        delpech-infra) is already at its concurrency cap ($COORD_MCP_CI_CONCURRENCY_LIMIT,
        default 2) — established 2026-08-27 after ~35 simultaneous CI-active work items
        OOMKilled it. checkin() never blocks on this (it only *declares*, it does not
        consume a CI slot) — but a 'WAIT' here is an early signal: don't rush to actually
        trigger CI (push/open a PR) once you start this work, the enforced gate is at
        `checkout_work`. Respect 'WAIT' — do not treat it as advisory-only noise; see
        ci_gate.wait_on for which work items to watch for `release_work`.
    """
    return await asyncio.to_thread(
        _checkin,
        repo_path=repo_path,
        title=title,
        scope_files=scope_files,
        scope_symbols=scope_symbols,
        scope_adr_topic=scope_adr_topic,
        milestone_number=milestone_number,
        eta_hours=eta_hours,
        agent_id=agent_id,
        triggers_ci=triggers_ci,
    )


@mcp.tool()
async def claim_issue(work_item_id: str, github_issue_number: int) -> dict[str, Any]:
    """Bind a work item to an EXISTING GitHub issue (assigns @me on GH)."""
    return await asyncio.to_thread(_claim_issue, work_item_id, github_issue_number)


@mcp.tool()
async def claim_new(
    work_item_id: str,
    body: str = "",
    labels: list[str] | None = None,
) -> dict[str, Any]:
    """Create a NEW GitHub issue and bind the work item to it."""
    return await asyncio.to_thread(_claim_new, work_item_id, body=body, labels=labels)


# ── Arrival gate ─────────────────────────────────────────────────────


@mcp.tool()
async def checkout_work(
    work_item_id: str,
    diff_files: list[str] | None = None,
    auto_detect_diff: bool = True,
    worktree_path: str | None = None,
) -> dict[str, Any]:
    """Arrival gate before merge. Validates scope match, detects parallel conflicts.

    Returns ready_to_merge: bool, warnings: [...], blockers: [...],
    acceptance_criteria_status: {total, checked, unchecked, all_checked},
    open_pr_conflicts_on_files, diff_source.

    ALSO enforces the GLOBAL (cross-repo) CI-concurrency gate — `triggers_ci`
    (resolved at checkin) and `ci_gate` in the result. If `ci_gate.you_should
    == "WAIT"` (shared CI runner at cap — $COORD_MCP_CI_CONCURRENCY_LIMIT,
    default 2), status is NOT transitioned to 'checked_out': it stays at its
    prior status, `blockers` explains why, and `ready_to_merge` is False.
    THIS IS A MUST-RESPECT SIGNAL, not advisory noise — established
    2026-08-27 after Forgejo (shared by alert-immo AND delpech-infra)
    OOMKilled repeatedly from ~35 simultaneous CI-active work items (the K8s
    node itself was fine at 38% load; Forgejo's own process memory wasn't).
    Do not work around a 'WAIT' by pushing/opening a PR anyway — retry
    checkout_work after one of `ci_gate.wait_on`'s work items calls
    `release_work`, which frees a slot.

    Auto-detection cascade when `diff_files is None and auto_detect_diff`:
      1. `worktree_path` (explicit override — escape hatch).
      2. Scope-matching worktree (iterates `git worktree list`, picks the one
         whose diff vs the resolved reference branch best overlaps declared
         scope_files). Required for multi-worktree workflows (cf. coord-mcp#4).
      3. Fallback to repo_path HEAD (original behaviour). Emits a warning if
         HEAD == reference branch and diff is empty (cf. coord-mcp#2).

    The reference branch is auto-detected per repo, NOT hardcoded to
    `origin/main` — see `checkout.resolve_reference_ref` (fixes phantom scope
    conflicts on repos whose canonical remote isn't `origin`, e.g. Forgejo
    repos with a stale GitHub `origin` mirror). Override via git config
    `coord-mcp.canonical-remote` or the `COORD_MCP_REFERENCE_REMOTE` env var.
    """
    return await asyncio.to_thread(
        checkout,
        work_item_id,
        diff_files=diff_files,
        auto_detect_diff=auto_detect_diff,
        worktree_path=worktree_path,
    )


@mcp.tool()
async def release_work(
    work_item_id: str,
    outcome: str,
    close_github_issue: bool = False,
) -> dict[str, Any]:
    """Finalize a work item. Stores outcome, optionally closes the GH issue with comment."""
    return await asyncio.to_thread(
        release, work_item_id, outcome=outcome, close_github_issue=close_github_issue
    )


@mcp.tool()
async def abandon_work(work_item_id: str, reason: str = "") -> dict[str, Any]:
    """Mark a declared/claimed work item as abandoned (e.g. user changed mind)."""
    return await asyncio.to_thread(abandon, work_item_id, reason=reason)


@mcp.tool()
async def relink_issue(work_item_id: str, issue_number: int, note: str = "") -> dict[str, Any]:
    """Repoint a work item's issue_number without touching status or calling gh.

    For platform migrations (e.g. GitHub -> Forgejo, 04.08.2026) where the issue
    content already exists elsewhere under a new number.
    """
    return await asyncio.to_thread(_relink_issue, work_item_id, issue_number, note=note)


# ── Visibility ───────────────────────────────────────────────────────


@mcp.tool()
async def list_active_work(repo_path: str | None = None) -> list[dict[str, Any]]:
    """List every work item NOT in terminal status. Cross-repo by default."""
    return await asyncio.to_thread(_list_active_work, repo_path=repo_path)


@mcp.tool()
async def get_work(work_item_id: str) -> dict[str, Any] | None:
    """Fetch a single work item's full record."""
    return await asyncio.to_thread(get_work_item, work_item_id)


@mcp.tool()
async def plan_parallel_waves(repo_path: str) -> dict[str, Any]:
    """Partition every active work item into the minimum number of conflict-free waves.

    Greedy graph coloring on scope overlap (declared scope_files ∪ GitNexus-expanded
    scope_symbols): two items in the same wave are guaranteed disjoint and safe to
    run fully in parallel; items in different waves must be sequenced. Answers
    "how many agents can I actually run at once right now" for a given repo.
    """
    return await asyncio.to_thread(_plan_parallel_waves, repo_path=repo_path)


# ── ADR registry ─────────────────────────────────────────────────────


@mcp.tool()
async def claim_adr_number(
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
    return await asyncio.to_thread(
        claim_adr,
        repo_path=repo_path,
        topic=topic,
        work_item_id=work_item_id,
        allocated_to=allocated_to,
        create_skeleton=create_skeleton,
    )


@mcp.tool()
async def list_adr_allocations(repo_path: str | None = None) -> list[dict[str, Any]]:
    """Show all ADR allocations known to coord-mcp. Filter by repo if given."""
    return await asyncio.to_thread(list_allocations, repo_path=repo_path)


# ── Audit ────────────────────────────────────────────────────────────


def _audit_tail_sync(limit: int) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


@mcp.tool()
async def audit_tail(limit: int = 20) -> list[dict[str, Any]]:
    """Return the last N audit log entries (most recent first)."""
    return await asyncio.to_thread(_audit_tail_sync, limit)


# ── Entry point ──────────────────────────────────────────────────────


def main() -> None:
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
