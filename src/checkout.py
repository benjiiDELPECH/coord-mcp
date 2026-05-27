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
    worktree_path: str | None = None,
) -> dict:
    """Arrival gate: validate scope match, detect parallel conflicts, list AC.

    Auto-detection cascade (when diff_files is None and auto_detect_diff):
      1. Explicit `worktree_path` param (escape hatch) → use it as cwd.
      2. Scope-matching worktree: iterate `git worktree list`, return the
         worktree whose diff vs origin/main best overlaps declared scope.
         Resolves #4 (HEAD ambigu when working in dedicated worktrees).
      3. Fallback to repo_path HEAD (original behaviour). Emits an explicit
         warning if HEAD == origin/main (resolves #2: empty diff is suspicious).
    """
    with connection() as conn:
        row = conn.execute("SELECT * FROM work_items WHERE id = ?", (work_item_id,)).fetchone()
    if not row:
        return {"error": f"work_item {work_item_id} not found"}

    item = _row_to_dict(row)
    declared = set(item.get("scope_files") or [])
    repo_path = item["repo"]
    repo_slug = _detect_repo_slug(repo_path)

    warnings: list[str] = []
    blockers: list[str] = []
    diff_source = "explicit"  # explicit | worktree-override | scope-match | repo-fallback

    if diff_files is None and auto_detect_diff:
        diff_files, diff_source, detection_warnings = _auto_detect_diff_files(
            repo_path=repo_path,
            declared_files=declared,
            worktree_path_override=worktree_path,
        )
        warnings.extend(detection_warnings)
    diff_files = diff_files or []
    actual = set(diff_files)

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
        "diff_source": diff_source,
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
                    "diff_files_count": len(diff_files),
                    "worktree_path_override": worktree_path,
                    "diff_source": diff_source},
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
    """Files changed vs origin/main (fallback: vs HEAD~1, then staged+unstaged).

    Returns empty list if no changes found at any layer (caller should warn
    if this is unexpected — see issue #2).
    """
    for cmd in (
        ["git", "diff", "--name-only", "origin/main...HEAD"],
        ["git", "diff", "--name-only", "HEAD~1"],
        ["git", "diff", "--name-only"],
    ):
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=repo_path, timeout=10)
        if result.returncode == 0 and result.stdout.strip():
            return [f for f in result.stdout.strip().splitlines() if f]
    return []


def _list_worktrees(repo_path: str) -> list[dict]:
    """Return worktrees attached to this repo via `git worktree list --porcelain`.

    Output: [{"path": str, "branch": str | None, "head": str}, ...]. Empty on error.
    """
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True, text=True, cwd=repo_path, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        return []
    if result.returncode != 0:
        return []

    worktrees: list[dict] = []
    current: dict = {}
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            if current.get("path"):
                worktrees.append(current)
            current = {}
            continue
        if line.startswith("worktree "):
            current["path"] = line.removeprefix("worktree ").strip()
        elif line.startswith("HEAD "):
            current["head"] = line.removeprefix("HEAD ").strip()
        elif line.startswith("branch "):
            ref = line.removeprefix("branch ").strip()
            current["branch"] = ref.removeprefix("refs/heads/")
        elif line == "detached":
            current["branch"] = None
    if current.get("path"):
        worktrees.append(current)
    return worktrees


def _head_is_origin_main(repo_path: str) -> bool:
    """True iff HEAD of `repo_path` points to the same commit as origin/main.

    Used to emit a warning when auto-detect returns an empty diff because the
    user is sitting on `main` directly (resolves #2).
    """
    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", "origin/main...HEAD"],
            capture_output=True, text=True, cwd=repo_path, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        return False
    if result.returncode != 0:
        return False
    # rev-list --count A...B returns commits unique to either side; 0 = same commit.
    return result.stdout.strip() == "0"


def _find_worktree_matching_scope(
    repo_path: str,
    declared_files: set[str],
) -> tuple[str | None, int]:
    """Return (best_path, overlap_count) — worktree whose diff overlaps declared_files most.

    Returns (None, 0) if no worktree overlaps at all, or if declared_files is empty.
    """
    if not declared_files:
        return None, 0

    best_path: str | None = None
    best_overlap = 0
    for wt in _list_worktrees(repo_path):
        wt_path = wt.get("path")
        if not wt_path:
            continue
        wt_diff = set(_git_diff_files(wt_path))
        overlap = len(wt_diff & declared_files)
        if overlap > best_overlap:
            best_path, best_overlap = wt_path, overlap
    return best_path, best_overlap


def _auto_detect_diff_files(
    repo_path: str,
    declared_files: set[str],
    worktree_path_override: str | None = None,
) -> tuple[list[str], str, list[str]]:
    """Cascade: explicit override → scope-matching worktree → repo HEAD fallback.

    Returns (diff_files, diff_source, warnings).
    diff_source ∈ {"worktree-override", "scope-match", "repo-fallback"}.
    """
    warnings: list[str] = []

    # 1. Explicit override (escape hatch — power user, exotic setup).
    if worktree_path_override:
        return _git_diff_files(worktree_path_override), "worktree-override", warnings

    # 2. Scope-matching worktree (resolves #4 — multi-worktree workflow).
    matched_path, _overlap = _find_worktree_matching_scope(repo_path, declared_files)
    if matched_path and matched_path != repo_path:
        return _git_diff_files(matched_path), "scope-match", warnings

    # 3. Fallback: HEAD of the declared repo_path (original behaviour).
    diff = _git_diff_files(repo_path)

    # Issue #2 — warn if empty diff because we're sitting on origin/main.
    if not diff and _head_is_origin_main(repo_path):
        warnings.append(
            "HEAD of repo_path is at origin/main (no commits ahead) — diff is empty. "
            "If you expect changes, pass `diff_files` explicitly or commit/checkout "
            "to the feature branch first."
        )

    # Issue #4 (defensive) — declared scope given but fallback HEAD produced
    # nothing matching it → user is probably in another worktree.
    if declared_files and diff and not (declared_files & set(diff)):
        warnings.append(
            f"HEAD of repo_path produced a diff ({len(diff)} files) but NONE overlap "
            f"declared_files. You may be checking out from the wrong worktree — "
            f"consider passing `worktree_path` explicitly."
        )

    return diff, "repo-fallback", warnings


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
