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
import os
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
         worktree whose diff vs the resolved reference branch (see
         `resolve_reference_ref`) best overlaps declared scope.
         Resolves #4 (HEAD ambigu when working in dedicated worktrees).
      3. Fallback to repo_path HEAD (original behaviour). Emits an explicit
         warning if HEAD == reference branch (resolves #2: empty diff is
         suspicious).

    The reference branch is NOT hardcoded to `origin/main` — coord-mcp is
    shared across repos with different canonical remotes (e.g. a repo whose
    canonical remote is `forgejo`, with `origin` as a stale GitHub mirror).
    See `resolve_reference_ref` for the resolution order.
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


_DEFAULT_REFERENCE_REMOTE = "origin"
_DEFAULT_REFERENCE_BRANCH = "main"


def _list_remotes(repo_path: str) -> list[str]:
    """Return configured remote names (`git remote`). Empty list on any error."""
    try:
        result = subprocess.run(
            ["git", "remote"], capture_output=True, text=True, cwd=repo_path, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        return []
    if result.returncode != 0:
        return []
    return [r for r in result.stdout.strip().splitlines() if r]


def _remote_ref_if_exists(repo_path: str, remote: str) -> str | None:
    """Return '<remote>/main' or '<remote>/master' (whichever resolves), else None."""
    for branch in (_DEFAULT_REFERENCE_BRANCH, "master"):
        ref = f"{remote}/{branch}"
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet", ref],
                capture_output=True, text=True, cwd=repo_path, timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
            return None
        if result.returncode == 0 and result.stdout.strip():
            return ref
    return None


def _ref_commit_epoch(repo_path: str, ref: str) -> int:
    """Unix timestamp of the tip commit of `ref`, or -1 if unresolvable."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%ct", ref],
            capture_output=True, text=True, cwd=repo_path, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        return -1
    if result.returncode != 0 or not result.stdout.strip():
        return -1
    try:
        return int(result.stdout.strip().splitlines()[0])
    except ValueError:
        return -1


def resolve_reference_ref(repo_path: str) -> str:
    """Determine which `<remote>/<branch>` ref HEAD should be diffed against.

    Root cause this fixes: `checkout_work` used to hardcode `origin/main` as
    the diff baseline. coord-mcp is shared across MULTIPLE repos, and not
    every repo's canonical remote is `origin` — e.g. alert-immo's canonical
    remote is `forgejo`, with `origin` left behind as a stale GitHub mirror.
    Diffing against the stale mirror produced huge phantom diffs (hundreds of
    "conflicting" files that were actually just commits already on the real
    canonical branch), which `checkout()` then reported as bogus parallel
    scope conflicts. A hardcoded remote name is structurally wrong in a
    multi-repo tool — this makes the reference branch configurable/detected
    per repo instead.

    Resolution order (most to least specific):
      1. Per-repo git config `coord-mcp.canonical-remote`
         (`git config coord-mcp.canonical-remote forgejo` inside the repo) —
         explicit, versioned-in-intent, scoped to exactly this repo.
      2. `COORD_MCP_REFERENCE_REMOTE` env var — explicit global override,
         useful when many repos share the same non-`origin` convention.
      3. Auto-detect: among all configured remotes with a resolvable
         main/master ref, pick the one whose ref has the MOST RECENT commit
         timestamp. A stale mirror loses by construction — no hardcoded
         remote name required, and this degrades gracefully if the user
         never configured anything.
      4. Fallback: 'origin/main' — preserves original behaviour for the
         common case of a single standard remote (or when no remotes are
         configured, e.g. tests / bare working copies).
    """
    remotes = _list_remotes(repo_path)

    # 1. Per-repo git config override (most specific — wins).
    try:
        cfg = subprocess.run(
            ["git", "config", "--get", "coord-mcp.canonical-remote"],
            capture_output=True, text=True, cwd=repo_path, timeout=10,
        )
        cfg_remote = cfg.stdout.strip() if cfg.returncode == 0 else ""
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        cfg_remote = ""
    if cfg_remote and cfg_remote in remotes:
        ref = _remote_ref_if_exists(repo_path, cfg_remote)
        if ref:
            return ref

    # 2. Global env var override.
    env_remote = os.environ.get("COORD_MCP_REFERENCE_REMOTE")
    if env_remote and env_remote in remotes:
        ref = _remote_ref_if_exists(repo_path, env_remote)
        if ref:
            return ref

    # 3. Auto-detect: freshest remote ref wins.
    candidates: list[tuple[str, int]] = []
    for remote in remotes:
        ref = _remote_ref_if_exists(repo_path, remote)
        if ref:
            epoch = _ref_commit_epoch(repo_path, ref)
            if epoch >= 0:
                candidates.append((ref, epoch))
    if candidates:
        candidates.sort(key=lambda c: c[1], reverse=True)
        return candidates[0][0]

    # 4. Fallback — preserves original single-remote behaviour.
    return f"{_DEFAULT_REFERENCE_REMOTE}/{_DEFAULT_REFERENCE_BRANCH}"


def _git_diff_files(repo_path: str) -> list[str]:
    """Files changed vs the resolved reference branch (fallback: HEAD~1, then
    staged+unstaged).

    Returns empty list if no changes found at any layer, or if git fails for
    any reason (missing binary, stale NFS, non-git dir, timeout, etc.).
    Caller should warn if empty diff is unexpected — see issue #2.

    Safety: this function is called N times per `checkout()` when iterating
    worktrees (see `_find_worktree_matching_scope`). A single bad worktree
    (dead NFS mount, broken symlink, missing git) MUST NOT crash the whole
    cascade — hence the broad try/except wrapping `subprocess.run`.
    """
    reference_ref = resolve_reference_ref(repo_path)
    for cmd in (
        ["git", "diff", "--name-only", f"{reference_ref}...HEAD"],
        ["git", "diff", "--name-only", "HEAD~1"],
        ["git", "diff", "--name-only"],
    ):
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, cwd=repo_path, timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
            # One layer failed (binary missing, cwd gone, timeout) — try next.
            continue
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
    """True iff HEAD of `repo_path` points to the same commit as the resolved
    reference branch (see `resolve_reference_ref` — NOT hardcoded to origin/main,
    despite the function name kept for backward compatibility).

    Used to emit a warning when auto-detect returns an empty diff because the
    user is sitting on the reference branch directly (resolves #2).
    """
    reference_ref = resolve_reference_ref(repo_path)
    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", f"{reference_ref}...HEAD"],
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
) -> tuple[str | None, int, list[str]]:
    """Return (best_path, overlap_count, ambiguous_candidates).

    Three cases:
      - **Unique best**:   (path, count, [])             — single winner
      - **Tied best**:     (None, count, sorted([paths])) — multiple at same overlap
      - **No overlap**:    (None, 0, [])                 — nothing matched
      - **Empty declared**: (None, 0, [])                — can't match

    Tie detection is intentional: `git worktree list` order is
    filesystem-dependent (creation order on most FS, not guaranteed). Silently
    picking the first would produce "works on my machine" bugs. Caller is
    expected to surface the ambiguity as a warning and fall through to
    `repo-fallback`.
    """
    if not declared_files:
        return None, 0, []

    candidates: list[tuple[str, int]] = []
    for wt in _list_worktrees(repo_path):
        wt_path = wt.get("path")
        if not wt_path:
            continue
        wt_diff = set(_git_diff_files(wt_path))
        overlap = len(wt_diff & declared_files)
        if overlap > 0:
            candidates.append((wt_path, overlap))

    if not candidates:
        return None, 0, []

    max_overlap = max(c[1] for c in candidates)
    tied = sorted(path for path, ov in candidates if ov == max_overlap)

    if len(tied) == 1:
        return tied[0], max_overlap, []
    return None, max_overlap, tied


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
    matched_path, max_overlap, ambiguous = _find_worktree_matching_scope(
        repo_path, declared_files,
    )
    if matched_path and matched_path != repo_path:
        return _git_diff_files(matched_path), "scope-match", warnings
    # Tie detected — multiple worktrees overlap declared scope equally.
    # Fall through to repo-fallback with explicit warning naming the candidates.
    if ambiguous:
        warnings.append(
            f"Multiple worktrees tie for declared scope overlap ({max_overlap} "
            f"file(s) each): {ambiguous}. Pass `worktree_path` explicitly to "
            f"disambiguate. Falling back to repo_path HEAD."
        )

    # 3. Fallback: HEAD of the declared repo_path (original behaviour).
    diff = _git_diff_files(repo_path)

    # Issue #2 — warn if empty diff because we're sitting on the reference branch.
    if not diff and _head_is_origin_main(repo_path):
        reference_ref = resolve_reference_ref(repo_path)
        warnings.append(
            f"HEAD of repo_path is at {reference_ref} (no commits ahead) — diff is empty. "
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
