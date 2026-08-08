"""
Checkout & release — arrival gate before merge.

checkout(work_item_id, diff_files?) :
    - Compares actual modified files to declared scope_files (mismatch warning).
    - Searches for OTHER active work_items whose scope overlaps the actual diff
      (parallel work that might conflict at merge time).
    - Detects open PRs touching the same files (cross-team coordination).
    - Returns ready_to_merge boolean + warnings + blockers.

release(work_item_id, outcome, close_github_issue?) :
    - Marks the work_item as released, stores outcome summary.
    - Optionally closes the linked GitHub issue with a comment.
    - Audit-logs the closure.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from . import graphiti_bridge
from .db import connection, log_audit, now_iso
from .work_items import _detect_repo_slug, _gh, _row_to_dict, ACTIVE_STATUSES


def checkout(
    work_item_id: str,
    diff_files: list[str] | None = None,
    auto_detect_diff: bool = True,
) -> dict:
    """Arrival gate: validate scope match, detect parallel conflicts, list AC."""
    with connection() as conn:
        row = conn.execute("SELECT * FROM work_items WHERE id = ?", (work_item_id,)).fetchone()
    if not row:
        return {"error": f"work_item {work_item_id} not found"}

    item = _row_to_dict(row)
    declared = set(item.get("scope_files") or [])
    repo_path = item["repo"]
    repo_slug = _detect_repo_slug(repo_path)

    if diff_files is None and auto_detect_diff:
        diff_files = _git_diff_files(repo_path)
    diff_files = diff_files or []
    actual = set(diff_files)

    warnings: list[str] = []
    blockers: list[str] = []

    # 1. Scope mismatch
    out_of_scope = actual - declared if declared else set()
    missed = declared - actual if declared else set()
    if declared and out_of_scope:
        warnings.append(f"Out-of-scope edits ({len(out_of_scope)} files): {sorted(out_of_scope)[:5]}…")
    if declared and missed:
        warnings.append(f"Declared but untouched ({len(missed)} files): {sorted(missed)[:5]}…")

    # 2. Conflict detection vs OTHER active work items
    parallel_conflicts = []
    with connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM work_items WHERE repo = ? AND id != ? AND status IN ({','.join('?' * len(ACTIVE_STATUSES))})",
            (repo_path, work_item_id, *ACTIVE_STATUSES),
        ).fetchall()
    for other in rows:
        other_files = set(json.loads(other["scope_files"] or "[]"))
        overlap = actual & other_files
        if overlap:
            parallel_conflicts.append({
                "work_item_id": other["id"],
                "title": other["title"],
                "github_issue_number": other["github_issue_number"],
                "overlapping_files": sorted(overlap),
            })
    if parallel_conflicts:
        blockers.append(f"{len(parallel_conflicts)} parallel work item(s) touch the same files — coordinate before merge")

    # 3. Open PRs on the same files
    pr_conflicts = _find_open_prs_on_files(repo_slug, diff_files) if repo_slug else []
    if pr_conflicts:
        warnings.append(f"{len(pr_conflicts)} open PR(s) modify the same files")

    # 4. AC checklist (from linked issue body)
    ac_status = None
    if item.get("github_issue_number") and repo_slug:
        ac_status = _parse_acceptance_criteria(repo_slug, item["github_issue_number"])

    ready_to_merge = not blockers

    with connection() as conn:
        conn.execute(
            "UPDATE work_items SET status='checked_out', updated_at=? WHERE id=?",
            (now_iso(), work_item_id),
        )

    result = {
        "work_item_id": work_item_id,
        "ready_to_merge": ready_to_merge,
        "declared_files": sorted(declared),
        "actual_diff_files": sorted(actual),
        "out_of_scope_count": len(out_of_scope),
        "untouched_declared_count": len(missed),
        "parallel_conflicts": parallel_conflicts,
        "open_pr_conflicts_on_files": pr_conflicts,
        "acceptance_criteria_status": ac_status,
        "warnings": warnings,
        "blockers": blockers,
        "status": "checked_out",
    }
    log_audit("checkout",
              args={"work_item_id": work_item_id, "auto_detect_diff": auto_detect_diff,
                    "diff_files_count": len(diff_files)},
              result=result, work_item_id=work_item_id, agent_id=item.get("agent_id"))
    return result


def release(
    work_item_id: str,
    outcome: str,
    close_github_issue: bool = False,
) -> dict:
    """Mark work item as released; optionally close the GH issue with outcome."""
    with connection() as conn:
        row = conn.execute("SELECT * FROM work_items WHERE id = ?", (work_item_id,)).fetchone()
    if not row:
        return {"error": f"work_item {work_item_id} not found"}

    item = _row_to_dict(row)
    repo_slug = _detect_repo_slug(item["repo"])

    closed = False
    if close_github_issue and item.get("github_issue_number") and repo_slug:
        comment = f"**Released by coord-mcp** ({work_item_id})\n\n{outcome}"
        _gh(["issue", "close", str(item["github_issue_number"]),
             "--repo", repo_slug, "--comment", comment], json_out=False)
        closed = True

    with connection() as conn:
        conn.execute(
            "UPDATE work_items SET status='released', outcome=?, updated_at=? WHERE id=?",
            (outcome, now_iso(), work_item_id),
        )

    graphiti_group_id = graphiti_bridge.infer_group_id(item["repo"])
    if graphiti_group_id:
        graphiti_result = graphiti_bridge.persist_outcome(item["title"], outcome, graphiti_group_id)
    else:
        graphiti_result = {"persisted": False, "warnings": ["repo has no known Graphiti group_id — skipped"]}

    result = {
        "work_item_id": work_item_id,
        "status": "released",
        "github_issue_closed": closed,
        "outcome": outcome[:200],
        "graphiti_persisted": graphiti_result["persisted"],
        "graphiti_warnings": graphiti_result["warnings"],
    }
    log_audit("release",
              args={"work_item_id": work_item_id, "close_github_issue": close_github_issue,
                    "outcome_preview": (outcome or "")[:120]},
              result=result, work_item_id=work_item_id, agent_id=item.get("agent_id"))
    return result


def abandon(work_item_id: str, reason: str = "") -> dict:
    """Mark a work item as abandoned (declared but never claimed/done)."""
    with connection() as conn:
        row = conn.execute("SELECT agent_id FROM work_items WHERE id = ?", (work_item_id,)).fetchone()
        cur = conn.execute(
            "UPDATE work_items SET status='abandoned', outcome=?, updated_at=? WHERE id=?",
            (f"abandoned: {reason}", now_iso(), work_item_id),
        )
    if cur.rowcount == 0:
        return {"error": f"work_item {work_item_id} not found"}
    result = {"work_item_id": work_item_id, "status": "abandoned", "reason": reason}
    log_audit("abandon",
              args={"work_item_id": work_item_id, "reason": reason},
              result=result, work_item_id=work_item_id,
              agent_id=row["agent_id"] if row else None)
    return result


# ── Helpers ──────────────────────────────────────────────────────────


def _git_diff_files(repo_path: str) -> list[str]:
    """Files changed vs origin/main (fallback: vs HEAD~1)."""
    for cmd in (
        ["git", "diff", "--name-only", "origin/main...HEAD"],
        ["git", "diff", "--name-only", "HEAD~1"],
        ["git", "diff", "--name-only"],
    ):
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=repo_path, timeout=10)
        if result.returncode == 0 and result.stdout.strip():
            return [f for f in result.stdout.strip().splitlines() if f]
    return []


def _find_open_prs_on_files(repo_slug: str, files: list[str]) -> list[dict]:
    """Find open PRs whose changed files overlap with `files`."""
    if not repo_slug or not files:
        return []
    target = set(files)
    prs = _gh(["pr", "list", "--repo", repo_slug, "--state", "open",
               "--limit", "30", "--json", "number,title,url,files"])
    if not isinstance(prs, list):
        return []
    hits = []
    for pr in prs:
        pr_files = {f["path"] for f in pr.get("files", [])}
        overlap = target & pr_files
        if overlap:
            hits.append({
                "number": pr["number"],
                "title": pr["title"],
                "url": pr["url"],
                "overlapping_files": sorted(overlap),
            })
    return hits


def _parse_acceptance_criteria(repo_slug: str, issue_number: int) -> dict:
    """Parse `- [ ]` / `- [x]` lines from the issue body."""
    data = _gh(["issue", "view", str(issue_number), "--repo", repo_slug, "--json", "body"])
    if not isinstance(data, dict):
        return {"error": "could not fetch issue body"}
    body = data.get("body", "")
    checked = body.count("- [x]") + body.count("- [X]")
    unchecked = body.count("- [ ]")
    total = checked + unchecked
    return {
        "total": total,
        "checked": checked,
        "unchecked": unchecked,
        "all_checked": total > 0 and unchecked == 0,
    }
